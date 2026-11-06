"""Long-running worker process for the PostgreSQL-backed agent queue."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from typing import Any

from .contracts import CodingTask
from .queue import PostgresTaskQueue
from .runtime import build_orchestrator


async def run_worker() -> None:
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required by the agent worker")
    worker_id = os.getenv("RUSA_WORKER_ID", f"{socket.gethostname()}-worker")
    queue = PostgresTaskQueue(database_url)
    orchestrator = build_orchestrator()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    lease_seconds = int(os.getenv("RUSA_WORKER_LEASE_SECONDS", "1800"))
    if not 30 <= lease_seconds <= 86400:
        raise RuntimeError("RUSA_WORKER_LEASE_SECONDS must be between 30 and 86400")

    async def run_with_lease(task, tenant_id, task_id):
        async def renew_lease():
            while True:
                await asyncio.sleep(max(5, lease_seconds // 3))
                renewed = await asyncio.to_thread(
                    queue.heartbeat, tenant_id, task_id, worker_id, lease_seconds
                )
                if not renewed:
                    raise RuntimeError("worker lease was lost before task completion")

        work = asyncio.create_task(orchestrator.run(task))
        heartbeat = asyncio.create_task(renew_lease())
        try:
            done, _ = await asyncio.wait(
                {work, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                error = heartbeat.exception()
                if error is not None:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    raise error
                raise RuntimeError("worker lease heartbeat stopped unexpectedly")
            return await work
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

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
            record = await run_with_lease(task, tenant_id, task_id)
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
                    raise RuntimeError("worker lease expired before task completion was committed")
            else:
                failed = await asyncio.to_thread(
                    queue.fail,
                    tenant_id,
                    task_id,
                    worker_id,
                    "; ".join(record.errors) or f"task ended in {record.state.value}",
                    record.state.value == "failed",
                )
                if not failed:
                    raise RuntimeError("worker lease expired before task failure was committed")
        except Exception as exc:
            await asyncio.to_thread(
                queue.fail,
                tenant_id,
                task_id,
                worker_id,
                f"{type(exc).__name__}: {str(exc)[:1000]}",
                True,
            )


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
