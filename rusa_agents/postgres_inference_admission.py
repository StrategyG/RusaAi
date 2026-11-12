"""PostgreSQL-backed FIFO admission shared by all API processes."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from uuid import UUID, uuid4

from .inference_admission import (
    InferenceDeadlineTimeout,
    InferenceQueueFull,
    InferenceQueueTimeout,
)
from .metrics import (
    INFERENCE_ACTIVE_GENERATIONS,
    INFERENCE_QUEUE_REJECTIONS,
    INFERENCE_QUEUE_WAIT_SECONDS,
    INFERENCE_WAITING_REQUESTS,
)

logger = logging.getLogger(__name__)


class PostgresInferenceAdmissionController:
    """Durable FIFO admission with database leases and cross-process slot ownership.

    The database stores request IDs and scheduling metadata only. Prompt and
    completion data never enter the scheduler tables.
    """

    def __init__(
        self,
        dsn: str,
        max_active: int = 1,
        max_waiting: int = 3,
        queue_timeout: float = 60.0,
        lease_seconds: int = 30,
        poll_interval: float = 0.05,
    ) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        if not 1 <= max_active <= 256 or not 0 <= max_waiting <= 1024:
            raise ValueError("Inference admission limits are outside the allowed range")
        if not 0.1 <= queue_timeout <= 120:
            raise ValueError("Inference queue timeout must be between 0.1 and 120 seconds")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("Inference lease must be between 5 and 3600 seconds")
        if not 0.01 <= poll_interval <= 1:
            raise ValueError("Inference scheduler poll interval must be between 0.01 and 1 second")
        self.dsn = dsn
        self.max_active = max_active
        self.max_waiting = max_waiting
        self.queue_timeout = queue_timeout
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self._request_context: contextvars.ContextVar[UUID | None] = contextvars.ContextVar(
            f"rusa_inference_request_{id(self)}", default=None
        )
        self._owner_ids: dict[UUID, UUID] = {}
        self._owner_tasks: dict[UUID, asyncio.Task] = {}
        self._heartbeat_tasks: dict[UUID, asyncio.Task] = {}
        self._lease_lost: set[UUID] = set()
        self._local_active = 0
        self._local_waiting = 0

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Install the PostgreSQL runtime dependencies to use durable inference admission"
            ) from exc
        return psycopg.connect(
            self.dsn,
            connect_timeout=5,
            application_name="rusa-inference-admission",
            options=(
                "-c lock_timeout=5000 "
                "-c statement_timeout=5000 "
                "-c idle_in_transaction_session_timeout=10000"
            ),
        )

    @staticmethod
    def _lock_scheduler(conn) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('rusa.inference.admission', 0))"
        )

    def initialize(self) -> None:
        """Create development tables or fail closed when production migration 010 is absent."""
        production = os.getenv("ENVIRONMENT", "development").lower() == "production"
        with self._connect() as conn:
            if production:
                tables = conn.execute(
                    """
                    SELECT
                        to_regclass('public.rusa_inference_admission_config'),
                        to_regclass('public.rusa_inference_admission_requests'),
                        to_regclass('public.rusa_inference_admission_slots')
                    """
                ).fetchone()
                if not tables or any(table is None for table in tables):
                    raise RuntimeError(
                        "Production inference scheduler schema is missing; "
                        "apply migrations/010_inference_admission.sql"
                    )
            else:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.rusa_inference_admission_config (
                        singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
                        max_active SMALLINT NOT NULL CHECK (max_active BETWEEN 1 AND 256),
                        max_waiting INTEGER NOT NULL CHECK (max_waiting BETWEEN 0 AND 1024),
                        queue_timeout_ms INTEGER NOT NULL
                            CHECK (queue_timeout_ms BETWEEN 100 AND 120000),
                        lease_seconds INTEGER NOT NULL CHECK (lease_seconds BETWEEN 5 AND 3600),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.rusa_inference_admission_requests (
                        request_id UUID PRIMARY KEY,
                        owner_id UUID NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'waiting', 'active', 'released', 'cancelled',
                                'timed_out', 'expired'
                            )
                        ),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                        queue_expires_at TIMESTAMPTZ NOT NULL,
                        lease_owner UUID,
                        slot_id SMALLINT,
                        error_code TEXT,
                        CHECK (
                            (status = 'active' AND lease_owner IS NOT NULL AND slot_id IS NOT NULL)
                            OR
                            (status <> 'active' AND lease_owner IS NULL AND slot_id IS NULL)
                        )
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_rusa_inference_admission_waiting
                    ON public.rusa_inference_admission_requests(created_at, request_id)
                    WHERE status = 'waiting'
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_rusa_inference_admission_history
                    ON public.rusa_inference_admission_requests(updated_at)
                    WHERE status IN ('released', 'cancelled', 'timed_out', 'expired')
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.rusa_inference_admission_slots (
                        slot_id SMALLINT PRIMARY KEY CHECK (slot_id BETWEEN 0 AND 255),
                        active_request_id UUID UNIQUE
                            REFERENCES public.rusa_inference_admission_requests(request_id),
                        lease_owner UUID,
                        lease_expires_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                        CHECK (
                            (active_request_id IS NULL AND lease_owner IS NULL
                                AND lease_expires_at IS NULL)
                            OR
                            (active_request_id IS NOT NULL AND lease_owner IS NOT NULL
                                AND lease_expires_at IS NOT NULL)
                        )
                    )
                    """
                )

            expected = (
                self.max_active,
                self.max_waiting,
                int(round(self.queue_timeout * 1000)),
                self.lease_seconds,
            )
            with conn.transaction():
                self._lock_scheduler(conn)
                if not production:
                    conn.execute(
                        """
                        INSERT INTO public.rusa_inference_admission_config
                            (singleton_id, max_active, max_waiting, queue_timeout_ms, lease_seconds)
                        VALUES (1, %s, %s, %s, %s)
                        ON CONFLICT (singleton_id) DO NOTHING
                        """,
                        expected,
                    )
                configured = conn.execute(
                    """
                    SELECT max_active, max_waiting, queue_timeout_ms, lease_seconds
                    FROM public.rusa_inference_admission_config
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                ).fetchone()
                if configured is None:
                    raise RuntimeError(
                        "Inference scheduler configuration row is missing; "
                        "apply migrations/010_inference_admission.sql"
                    )
                if tuple(configured) != expected:
                    if production:
                        raise RuntimeError(
                            "Inference scheduler settings do not match the database config row; "
                            "align max_active, max_waiting, queue timeout, and lease_seconds "
                            "before starting additional API replicas"
                        )
                    active = conn.execute(
                        """
                        SELECT count(*) FROM public.rusa_inference_admission_slots
                        WHERE active_request_id IS NOT NULL
                        """
                    ).fetchone()[0]
                    if active:
                        raise RuntimeError(
                            "Cannot change development inference scheduler settings while slots are active"
                        )
                    conn.execute(
                        """
                        UPDATE public.rusa_inference_admission_config
                        SET max_active=%s, max_waiting=%s, queue_timeout_ms=%s,
                            lease_seconds=%s, updated_at=clock_timestamp()
                        WHERE singleton_id=1
                        """,
                        expected,
                    )

                conn.execute(
                    """
                    INSERT INTO public.rusa_inference_admission_slots(slot_id)
                    SELECT generate_series(0, %s - 1)::smallint
                    ON CONFLICT (slot_id) DO NOTHING
                    """,
                    (self.max_active,),
                )
                if not production:
                    active_excess = conn.execute(
                        """
                        SELECT count(*) FROM public.rusa_inference_admission_slots
                        WHERE slot_id >= %s AND active_request_id IS NOT NULL
                        """,
                        (self.max_active,),
                    ).fetchone()[0]
                    if active_excess:
                        raise RuntimeError(
                            "Cannot reduce development max_active while excess slots are active"
                        )
                    conn.execute(
                        "DELETE FROM public.rusa_inference_admission_slots WHERE slot_id >= %s",
                        (self.max_active,),
                    )
                configured_slots = conn.execute(
                    """
                    SELECT count(*) FROM public.rusa_inference_admission_slots
                    WHERE slot_id < %s
                    """,
                    (self.max_active,),
                ).fetchone()[0]
                if configured_slots != self.max_active:
                    raise RuntimeError(
                        "Inference scheduler slot rows do not match max_active; "
                        "provision the missing slots before starting API replicas"
                    )
                excess = conn.execute(
                    """
                    SELECT count(*) FROM public.rusa_inference_admission_slots
                    WHERE slot_id >= %s
                    """,
                    (self.max_active,),
                ).fetchone()[0]
                if excess:
                    raise RuntimeError(
                        "Inference scheduler has more slots than configured max_active; "
                        "drain traffic and reconcile the scheduler configuration"
                    )

                if production:
                    privilege_rows = conn.execute(
                        """
                        SELECT
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_config', 'SELECT'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_config', 'UPDATE'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_requests', 'SELECT'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_requests', 'INSERT'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_requests', 'UPDATE'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_requests', 'DELETE'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_slots', 'SELECT'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_slots', 'INSERT'),
                            has_table_privilege(current_user,
                                'public.rusa_inference_admission_slots', 'UPDATE')
                        """
                    ).fetchone()
                    if not privilege_rows or not all(privilege_rows):
                        raise RuntimeError(
                            "API database role lacks inference scheduler privileges; "
                            "apply migrations/010_inference_admission.sql"
                        )

    def _expire_stale(self, conn) -> None:
        stale_slots = conn.execute(
            """
            SELECT slot_id, active_request_id, lease_owner
            FROM public.rusa_inference_admission_slots
            WHERE active_request_id IS NOT NULL
              AND lease_expires_at <= clock_timestamp()
            FOR UPDATE
            """
        ).fetchall()
        for slot_id, request_id, owner_id in stale_slots:
            conn.execute(
                """
                UPDATE public.rusa_inference_admission_requests
                SET status='expired', lease_owner=NULL, slot_id=NULL,
                    error_code='lease_expired', updated_at=clock_timestamp()
                WHERE request_id=%s AND status='active' AND lease_owner=%s
                """,
                (request_id, owner_id),
            )
            conn.execute(
                """
                UPDATE public.rusa_inference_admission_slots
                SET active_request_id=NULL, lease_owner=NULL, lease_expires_at=NULL,
                    updated_at=clock_timestamp()
                WHERE slot_id=%s AND active_request_id=%s AND lease_owner=%s
                """,
                (slot_id, request_id, owner_id),
            )
        conn.execute(
            """
            UPDATE public.rusa_inference_admission_requests
            SET status='timed_out', error_code='queue_timeout',
                updated_at=clock_timestamp()
            WHERE status='waiting' AND queue_expires_at <= clock_timestamp()
            """
        )

    def _cleanup_history(self, conn) -> None:
        conn.execute(
            """
            DELETE FROM public.rusa_inference_admission_requests
            WHERE status IN ('released', 'cancelled', 'timed_out', 'expired')
              AND updated_at < clock_timestamp() - INTERVAL '1 day'
            """
        )

    def _enqueue(self, request_id: UUID, owner_id: UUID, wait_seconds: float) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                self._lock_scheduler(conn)
                self._expire_stale(conn)
                free_slot = conn.execute(
                    """
                    SELECT slot_id
                    FROM public.rusa_inference_admission_slots
                    WHERE active_request_id IS NULL
                    ORDER BY slot_id
                    LIMIT 1
                    FOR UPDATE
                    """
                ).fetchone()
                waiting_count = conn.execute(
                    """
                    SELECT count(*) FROM public.rusa_inference_admission_requests
                    WHERE status='waiting'
                    """
                ).fetchone()[0]
                if free_slot is not None and waiting_count == 0:
                    slot_id = free_slot[0]
                    conn.execute(
                        """
                        INSERT INTO public.rusa_inference_admission_requests
                            (request_id, owner_id, status, queue_expires_at,
                             lease_owner, slot_id)
                        VALUES (%s, %s, 'active',
                                clock_timestamp() + (%s * INTERVAL '1 second'),
                                %s, %s)
                        """,
                        (request_id, owner_id, wait_seconds, owner_id, slot_id),
                    )
                    conn.execute(
                        """
                        UPDATE public.rusa_inference_admission_slots
                        SET active_request_id=%s, lease_owner=%s,
                            lease_expires_at=clock_timestamp() + (%s * INTERVAL '1 second'),
                            updated_at=clock_timestamp()
                        WHERE slot_id=%s
                        """,
                        (request_id, owner_id, self.lease_seconds, slot_id),
                    )
                    self._cleanup_history(conn)
                    return True

                if waiting_count >= self.max_waiting:
                    raise InferenceQueueFull(
                        "Inference capacity is full; retry later"
                    )
                conn.execute(
                    """
                    INSERT INTO public.rusa_inference_admission_requests
                        (request_id, owner_id, status, queue_expires_at)
                    VALUES (%s, %s, 'waiting',
                            clock_timestamp() + (%s * INTERVAL '1 second'))
                    """,
                    (request_id, owner_id, wait_seconds),
                )
                self._cleanup_history(conn)
                return False

    def _try_claim(self, request_id: UUID, owner_id: UUID) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                self._lock_scheduler(conn)
                self._expire_stale(conn)
                current = conn.execute(
                    """
                    SELECT status, owner_id, lease_owner
                    FROM public.rusa_inference_admission_requests
                    WHERE request_id=%s
                    """,
                    (request_id,),
                ).fetchone()
                if current is None:
                    return False
                if current[0] == "active":
                    return current[1] == owner_id and current[2] == owner_id
                if current[0] != "waiting":
                    return False

                free_slot = conn.execute(
                    """
                    SELECT slot_id
                    FROM public.rusa_inference_admission_slots
                    WHERE active_request_id IS NULL
                    ORDER BY slot_id
                    LIMIT 1
                    FOR UPDATE
                    """
                ).fetchone()
                if free_slot is None:
                    return False
                oldest = conn.execute(
                    """
                    SELECT request_id, owner_id
                    FROM public.rusa_inference_admission_requests
                    WHERE status='waiting' AND queue_expires_at > clock_timestamp()
                    ORDER BY created_at, request_id
                    LIMIT 1
                    FOR UPDATE
                    """
                ).fetchone()
                if oldest is None:
                    return False

                claimed_request, claimed_owner = oldest
                slot_id = free_slot[0]
                conn.execute(
                    """
                    UPDATE public.rusa_inference_admission_requests
                    SET status='active', lease_owner=%s, slot_id=%s,
                        updated_at=clock_timestamp()
                    WHERE request_id=%s AND status='waiting'
                    """,
                    (claimed_owner, slot_id, claimed_request),
                )
                conn.execute(
                    """
                    UPDATE public.rusa_inference_admission_slots
                    SET active_request_id=%s, lease_owner=%s,
                        lease_expires_at=clock_timestamp() + (%s * INTERVAL '1 second'),
                        updated_at=clock_timestamp()
                    WHERE slot_id=%s AND active_request_id IS NULL
                    """,
                    (claimed_request, claimed_owner, self.lease_seconds, slot_id),
                )
                return claimed_request == request_id and claimed_owner == owner_id

    def _get_status(self, request_id: UUID, owner_id: UUID) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status, owner_id FROM public.rusa_inference_admission_requests
                WHERE request_id=%s
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            return "missing"
        if row[1] != owner_id:
            raise RuntimeError("Inference scheduler request ownership mismatch")
        return row[0]

    def _renew(self, request_id: UUID, owner_id: UUID) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                # Keep the same advisory-lock-first order as _withdraw/_expire_stale.
                # Otherwise heartbeat (slot -> request) can deadlock with release (request -> slot).
                self._lock_scheduler(conn)
                slot = conn.execute(
                    """
                    UPDATE public.rusa_inference_admission_slots
                    SET lease_expires_at=clock_timestamp() + (%s * INTERVAL '1 second'),
                        updated_at=clock_timestamp()
                    WHERE active_request_id=%s AND lease_owner=%s
                      AND lease_expires_at > clock_timestamp()
                    RETURNING slot_id
                    """,
                    (self.lease_seconds, request_id, owner_id),
                ).fetchone()
                if slot is None:
                    return False
                conn.execute(
                    """
                    UPDATE public.rusa_inference_admission_requests
                    SET updated_at=clock_timestamp()
                    WHERE request_id=%s AND status='active' AND lease_owner=%s
                    """,
                    (request_id, owner_id),
                )
                return True

    def _withdraw(self, request_id: UUID, owner_id: UUID, outcome: str) -> None:
        if outcome not in {"cancelled", "timed_out", "released"}:
            raise ValueError("Invalid inference admission terminal outcome")
        with self._connect() as conn:
            with conn.transaction():
                self._lock_scheduler(conn)
                row = conn.execute(
                    """
                    SELECT status, slot_id, lease_owner
                    FROM public.rusa_inference_admission_requests
                    WHERE request_id=%s AND owner_id=%s
                    FOR UPDATE
                    """,
                    (request_id, owner_id),
                ).fetchone()
                if row is None:
                    return
                status, slot_id, lease_owner = row
                if status == "active" and lease_owner == owner_id and slot_id is not None:
                    conn.execute(
                        """
                        UPDATE public.rusa_inference_admission_slots
                        SET active_request_id=NULL, lease_owner=NULL, lease_expires_at=NULL,
                            updated_at=clock_timestamp()
                        WHERE slot_id=%s AND active_request_id=%s AND lease_owner=%s
                        """,
                        (slot_id, request_id, owner_id),
                    )
                if status in {"waiting", "active"}:
                    conn.execute(
                        """
                        UPDATE public.rusa_inference_admission_requests
                        SET status=%s, lease_owner=NULL, slot_id=NULL,
                            error_code=%s, updated_at=clock_timestamp()
                        WHERE request_id=%s AND owner_id=%s AND status=%s
                        """,
                        (
                            outcome,
                            None if outcome == "released" else outcome,
                            request_id,
                            owner_id,
                            status,
                        ),
                    )

    async def _heartbeat(self, request_id: UUID, owner_id: UUID) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await asyncio.to_thread(self._renew, request_id, owner_id)
                except Exception:
                    logger.exception("Inference admission lease renewal failed")
                    renewed = False
                if not renewed:
                    logger.error("Inference admission lease lost; cancelling generation task")
                    self._lease_lost.add(request_id)
                    # StreamingResponse runs its body in a child task. Resolve the
                    # current owner at failure time so a stream can rebind it.
                    owner_task = self._owner_tasks.get(request_id)
                    if owner_task is not None and not owner_task.done():
                        owner_task.cancel()
                    return
        except asyncio.CancelledError:
            raise

    async def acquire(self, *, timeout_seconds: float | None = None) -> None:
        """Acquire the oldest available global slot, bounded by queue and request deadlines."""
        if timeout_seconds is not None and timeout_seconds <= 0:
            INFERENCE_QUEUE_REJECTIONS.labels(reason="deadline").inc()
            raise InferenceDeadlineTimeout("Inference request deadline expired")

        deadline_limited = (
            timeout_seconds is not None and timeout_seconds <= self.queue_timeout
        )
        wait_seconds = (
            self.queue_timeout
            if timeout_seconds is None
            else min(self.queue_timeout, timeout_seconds)
        )
        request_id = uuid4()
        owner_id = uuid4()
        queued_at = time.monotonic()
        enqueue_task = asyncio.create_task(
            asyncio.to_thread(self._enqueue, request_id, owner_id, wait_seconds)
        )
        try:
            immediate = await asyncio.shield(enqueue_task)
        except asyncio.CancelledError:
            await asyncio.gather(enqueue_task, return_exceptions=True)
            cleanup = asyncio.create_task(
                asyncio.to_thread(self._withdraw, request_id, owner_id, "cancelled")
            )
            await asyncio.shield(cleanup)
            INFERENCE_QUEUE_REJECTIONS.labels(reason="cancelled").inc()
            raise
        except InferenceQueueFull:
            INFERENCE_QUEUE_REJECTIONS.labels(reason="full").inc()
            raise

        if not immediate:
            self._local_waiting += 1
            INFERENCE_WAITING_REQUESTS.inc()
            try:
                while True:
                    await asyncio.to_thread(self._try_claim, request_id, owner_id)
                    current_status = await asyncio.to_thread(
                        self._get_status, request_id, owner_id
                    )
                    if current_status == "active":
                        break
                    if current_status == "timed_out":
                        INFERENCE_QUEUE_WAIT_SECONDS.observe(time.monotonic() - queued_at)
                        if deadline_limited:
                            INFERENCE_QUEUE_REJECTIONS.labels(reason="deadline").inc()
                            raise InferenceDeadlineTimeout(
                                "Inference request deadline expired while waiting for capacity"
                            )
                        INFERENCE_QUEUE_REJECTIONS.labels(reason="timeout").inc()
                        raise InferenceQueueTimeout(
                            "Timed out waiting for inference capacity"
                        )
                    if current_status != "waiting":
                        raise RuntimeError(
                            f"Inference admission request entered unexpected state: {current_status}"
                        )
                    remaining = wait_seconds - (time.monotonic() - queued_at)
                    if remaining <= 0:
                        await asyncio.to_thread(
                            self._withdraw, request_id, owner_id, "timed_out"
                        )
                        if deadline_limited:
                            INFERENCE_QUEUE_REJECTIONS.labels(reason="deadline").inc()
                            raise InferenceDeadlineTimeout(
                                "Inference request deadline expired while waiting for capacity"
                            )
                        INFERENCE_QUEUE_REJECTIONS.labels(reason="timeout").inc()
                        raise InferenceQueueTimeout(
                            "Timed out waiting for inference capacity"
                        )
                    await asyncio.sleep(min(self.poll_interval, remaining))
            except asyncio.CancelledError:
                INFERENCE_QUEUE_WAIT_SECONDS.observe(time.monotonic() - queued_at)
                cleanup = asyncio.create_task(
                    asyncio.to_thread(self._withdraw, request_id, owner_id, "cancelled")
                )
                await asyncio.shield(cleanup)
                INFERENCE_QUEUE_REJECTIONS.labels(reason="cancelled").inc()
                raise
            finally:
                self._local_waiting -= 1
                INFERENCE_WAITING_REQUESTS.dec()

        self._request_context.set(request_id)
        self._owner_ids[request_id] = owner_id
        owner_task = asyncio.current_task()
        if owner_task is None:
            await asyncio.to_thread(self._withdraw, request_id, owner_id, "cancelled")
            raise RuntimeError("Inference admission must be acquired from an asyncio task")
        self._owner_tasks[request_id] = owner_task
        self._heartbeat_tasks[request_id] = asyncio.create_task(
            self._heartbeat(request_id, owner_id)
        )
        self._local_active += 1
        INFERENCE_ACTIVE_GENERATIONS.inc()
        INFERENCE_QUEUE_WAIT_SECONDS.observe(time.monotonic() - queued_at)

    def bind_owner_task(self) -> bool:
        """Bind a streaming response body to the task that owns generation."""
        request_id = self._request_context.get()
        if request_id is None or request_id not in self._owner_ids:
            raise RuntimeError("Cannot bind a stream without an active inference lease")
        heartbeat = self._heartbeat_tasks.get(request_id)
        if request_id in self._lease_lost or heartbeat is None or heartbeat.done():
            return False
        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("Inference stream binding requires an asyncio task")
        self._owner_tasks[request_id] = owner_task
        return True

    async def release(self) -> None:
        """Release only the slot owned by the current request context."""
        request_id = self._request_context.get()
        if request_id is None:
            raise RuntimeError("Cannot release an inference slot that is not held")
        owner_id = self._owner_ids.get(request_id)
        if owner_id is None:
            raise RuntimeError("Inference admission request ownership is missing")

        heartbeat = self._heartbeat_tasks.pop(request_id, None)
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            await asyncio.to_thread(
                self._withdraw, request_id, owner_id, "released"
            )
        finally:
            self._request_context.set(None)
            self._owner_ids.pop(request_id, None)
            self._owner_tasks.pop(request_id, None)
            self._lease_lost.discard(request_id)
            self._lease_lost.discard(request_id)
            self._local_active = max(0, self._local_active - 1)
            INFERENCE_ACTIVE_GENERATIONS.dec()

    async def close(self) -> None:
        """Cancel locally owned requests and let their request tasks release leases."""
        for heartbeat in list(self._heartbeat_tasks.values()):
            heartbeat.cancel()
        if self._heartbeat_tasks:
            await asyncio.gather(*self._heartbeat_tasks.values(), return_exceptions=True)
        self._heartbeat_tasks.clear()

        current_task = asyncio.current_task()
        owner_tasks = [
            task
            for task in self._owner_tasks.values()
            if task is not current_task and not task.done()
        ]
        for owner_task in owner_tasks:
            owner_task.cancel()
        if owner_tasks:
            await asyncio.gather(*owner_tasks, return_exceptions=True)

        # A task that could not complete its finally block is cleaned up here.
        for request_id, owner_id in list(self._owner_ids.items()):
            try:
                await asyncio.to_thread(
                    self._withdraw, request_id, owner_id, "cancelled"
                )
            except Exception:
                logger.exception("Failed to release inference slot during shutdown")
            if self._owner_ids.pop(request_id, None) is not None:
                self._local_active = max(0, self._local_active - 1)
                INFERENCE_ACTIVE_GENERATIONS.dec()
            self._owner_tasks.pop(request_id, None)
