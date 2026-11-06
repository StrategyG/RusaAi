"""Queue consumer with lease renewal and graceful cancellation."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .contracts import CodingTask, RepositoryTarget, TaskState
from .orchestrator import AICodeOrchestrator
from .queue import Job, PostgresTaskQueue

logger = logging.getLogger("rusa-ai.aicode.worker")


class QueueWorker:
    def __init__(self, queue: PostgresTaskQueue, orchestrator: AICodeOrchestrator,
                 worker_id: str, poll_seconds: float = 2.0, lease_seconds: int = 180):
        self.queue = queue
        self.orchestrator = orchestrator
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    @staticmethod
    def _task_from_job(job: Job) -> CodingTask:
        data: dict[str, Any] = job.payload
        repo = data["repository"]
        return CodingTask(
            objective=data["objective"],
            context=data.get("context", ""),
            acceptance_criteria=data["acceptance_criteria"],
            task_id=job.task_id,
            repository=RepositoryTarget(
                full_name=repo["full_name"],
                base_branch=repo.get("base_branch", "main"),
                work_branch=repo["work_branch"],
            ),
            metadata={"tenant_id": job.tenant_id},
        )

    async def _renew_lease(self, job: Job) -> None:
        interval = max(5, self.lease_seconds // 3)
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            ok = await asyncio.to_thread(
                self.queue.heartbeat, job.id, job.tenant_id, self.worker_id, self.lease_seconds
            )
            if not ok:
                logger.error("Lost lease for job %s", job.id)
                return

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(self.queue.claim, self.worker_id, self.lease_seconds)
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._renew_lease(job))
        try:
            task = self._task_from_job(job)
            record = await self.orchestrator.run(task)
            if record.state == TaskState.SUCCEEDED:
                await asyncio.to_thread(self.queue.complete, job.id, job.tenant_id, self.worker_id)
            else:
                await asyncio.to_thread(
                    self.queue.fail, job.id, job.tenant_id, self.worker_id,
                    "; ".join(record.errors) or f"Task ended in {record.state.value}",
                )
        except Exception as exc:
            logger.exception("Worker failed job %s", job.id)
            await asyncio.to_thread(
                self.queue.fail, job.id, job.tenant_id, self.worker_id, str(exc)
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
