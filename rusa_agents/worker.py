"""Long-running worker process for the PostgreSQL-backed agent queue."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from typing import Any

from .contracts import CodingTask
from .runtime import build_orchestrator
from .queue import PostgresTaskQueue


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

    while not stopping.is_set():
        job = await asyncio.to_thread(queue.claim, worker_id, 1800)
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
            record = await orchestrator.run(task)
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
                await asyncio.to_thread(
                    queue.complete,
                    tenant_id,
                    task_id,
                    worker_id,
                    result,
                )
            else:
                await asyncio.to_thread(
                    queue.fail,
                    tenant_id,
                    task_id,
                    worker_id,
                    "; ".join(record.errors) or f"task ended in {record.state.value}",
                    True,
                )
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
