"""FIFO admission control for a bounded shared CPU inference slot."""
from __future__ import annotations

import asyncio
import time
from collections import deque

from .metrics import (
    INFERENCE_ACTIVE_GENERATIONS,
    INFERENCE_QUEUE_REJECTIONS,
    INFERENCE_QUEUE_WAIT_SECONDS,
    INFERENCE_WAITING_REQUESTS,
)


class InferenceQueueFull(RuntimeError):
    """Raised when the bounded inference waiting queue has no free position."""


class InferenceQueueTimeout(TimeoutError):
    """Raised when a request cannot obtain an inference slot before queue timeout."""


class InferenceDeadlineTimeout(TimeoutError):
    """Raised when a request-level deadline expires before inference admission."""


class InferenceAdmissionController:
    """Reserve active slots fairly and bound the number of waiting requests.

    The active count includes slots handed to waiters but not yet resumed. This
    prevents a timeout/cancellation race from leaking or double-releasing a slot.
    """

    def __init__(
        self,
        max_active: int = 1,
        max_waiting: int = 3,
        queue_timeout: float = 60.0,
    ) -> None:
        if not 1 <= max_active <= 256 or not 0 <= max_waiting <= 4096:
            raise ValueError("Inference admission limits are outside the allowed range")
        if not 0.1 <= queue_timeout <= 120:
            raise ValueError("Inference queue timeout must be between 0.1 and 120 seconds")
        self._max_active = max_active
        self._max_waiting = max_waiting
        self._queue_timeout = queue_timeout
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def waiting_count(self) -> int:
        return len(self._waiters)

    async def acquire(self, *, timeout_seconds: float | None = None) -> None:
        """Acquire a slot, optionally bounded by an earlier request deadline."""
        deadline_limited = (
            timeout_seconds is not None and timeout_seconds <= self._queue_timeout
        )
        if timeout_seconds is not None and timeout_seconds <= 0:
            INFERENCE_QUEUE_REJECTIONS.labels(reason="deadline").inc()
            raise InferenceDeadlineTimeout("Inference request deadline expired")

        wait_timeout = (
            self._queue_timeout
            if timeout_seconds is None
            else min(self._queue_timeout, timeout_seconds)
        )
        loop = asyncio.get_running_loop()
        queued_at = time.monotonic()
        async with self._lock:
            if self._active < self._max_active and not self._waiters:
                self._active += 1
                INFERENCE_ACTIVE_GENERATIONS.inc()
                INFERENCE_QUEUE_WAIT_SECONDS.observe(0.0)
                return
            if len(self._waiters) >= self._max_waiting:
                INFERENCE_QUEUE_REJECTIONS.labels(reason="full").inc()
                raise InferenceQueueFull("Inference capacity is full; retry later")
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append(waiter)
            INFERENCE_WAITING_REQUESTS.inc()

        try:
            await asyncio.wait_for(
                asyncio.shield(waiter),
                timeout=wait_timeout,
            )
            INFERENCE_QUEUE_WAIT_SECONDS.observe(time.monotonic() - queued_at)
        except asyncio.TimeoutError as exc:
            INFERENCE_QUEUE_WAIT_SECONDS.observe(time.monotonic() - queued_at)
            await self._withdraw(waiter)
            if deadline_limited:
                INFERENCE_QUEUE_REJECTIONS.labels(reason="deadline").inc()
                raise InferenceDeadlineTimeout(
                    "Inference request deadline expired while waiting for capacity"
                ) from exc
            INFERENCE_QUEUE_REJECTIONS.labels(reason="timeout").inc()
            raise InferenceQueueTimeout(
                "Timed out waiting for inference capacity"
            ) from exc
        except asyncio.CancelledError:
            INFERENCE_QUEUE_WAIT_SECONDS.observe(time.monotonic() - queued_at)
            INFERENCE_QUEUE_REJECTIONS.labels(reason="cancelled").inc()
            await self._withdraw(waiter)
            raise
        except Exception:
            await self._withdraw(waiter)
            raise

    async def release(self) -> None:
        async with self._lock:
            if self._active <= 0:
                raise RuntimeError("Cannot release an inference slot that is not held")
            self._release_locked()

    async def _withdraw(self, waiter: asyncio.Future[None]) -> None:
        async with self._lock:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                # The slot was already handed to this waiter. Return it so the
                # next queued request can proceed, even if this task was cancelled.
                if waiter.done() and not waiter.cancelled():
                    self._release_locked()
            else:
                INFERENCE_WAITING_REQUESTS.dec()
                if not waiter.done():
                    waiter.cancel()

    def _release_locked(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            INFERENCE_WAITING_REQUESTS.dec()
            if waiter.done():
                continue
            # Keep the active count unchanged: this slot is transferred FIFO.
            waiter.set_result(None)
            return
        self._active -= 1
        INFERENCE_ACTIVE_GENERATIONS.dec()
