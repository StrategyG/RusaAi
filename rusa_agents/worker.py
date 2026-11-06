"""Long-running queue worker with lease heartbeats and graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from typing import Any

from .contracts import CodingTask
from .queue import PostgresTaskQueue
from .runtime import build_orchestrator

logger = logging.getLogger("rusa-ai-worker")


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns the task lease."""


async def _run_with_heartbeat(
    orchestrator: Any,
    task: CodingTask,
    queue: PostgresTaskQueue,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    lease_seconds: int,
) -> Any:
    execution = asyncio.create_task(orchestrator.run(task))
    heartbeat_interval = min(30, max(5, lease_seconds // 3))
    while True:
        done, _ = await asyncio.wait({execution}, timeout=heartbeat_interval)
        if done:
            return await execution
        try:
            renewed = await asyncio.to_thread(
                queue.heartbeat,
                tenant_id,
                task_id,
                worker_id,
                lease_seconds,
            )
        except Exception as exc:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise LeaseLostError("could not renew the worker lease") from exc
        if not renewed:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise LeaseLostError("worker lease ownership was lost")


async def run_worker() -> None:
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required by the agent worker")
    worker_id = os.getenv("RUSA_WORKER_ID", f"{socket.gethostname()}-worker")
    lease_seconds = int(os.getenv("RUSA_WORKER_LEASE_SECONDS", "1800"))
    PostgresTaskQueue._validate_lease(lease_seconds)

    queue = PostgresTaskQueue(database_url)
    orchestrator = build_orchestrator()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    while not stopping.is_set():
        job = await asyncio.to_thread(queue.claim, worker_id, lease_seconds)
        if job is None:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except TimeoutError:
                pass
            continue

        tenant_id = job["tenant_id"]
        task_id = job["task_id"]
        try:
            task = CodingTask.from_dict(job["payload"], tenant_id=tenant_id)
            record = await _run_with_heartbeat(
                orchestrator,
                task,
                queue,
                tenant_id,
                task_id,
                worker_id,
                lease_seconds,
            )
            result: dict[str, Any] = {
                "state": record.state.value,
                "errors": record.errors,
                "artifacts": [
                    artifact
                    for worker_result in record.results
                    for artifact in worker_result.artifacts
                ],
            }
            if record.state.value == "succeeded":
                completed = await asyncio.to_thread(
                    queue.complete,
                    tenant_id,
                    task_id,
                    worker_id,
                    result,
                )
                if not completed:
                    logger.error("task completion rejected because worker lease is no longer valid")
            else:
                await asyncio.to_thread(
                    queue.fail,
                    tenant_id,
                    task_id,
                    worker_id,
                    "; ".join(record.errors) or f"task ended in {record.state.value}",
                    record.state.value == "failed",
                )
        except LeaseLostError:
            logger.warning("worker lost lease for task_id=%s; execution was cancelled", task_id)
        except Exception as exc:
            logger.exception("worker failed task_id=%s exception_type=%s", task_id, type(exc).__name__)
            try:
                await asyncio.to_thread(
                    queue.fail,
                    tenant_id,
                    task_id,
                    worker_id,
                    f"{type(exc).__name__}: {str(exc)[:1000]}",
                    True,
                )
            except Exception:
                logger.exception("could not persist worker failure for task_id=%s", task_id)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
