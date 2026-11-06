"""Worker process with lease renewal, durable checkpoints, and graceful shutdown."""

import asyncio
import logging
import os
import signal
import socket
import uuid

from .audit import HMACAuditLog
from .contracts import CodingTask, TaskRecord, TaskState, WorkerRole
from .github import GitHubToolGateway
from .memory import PostgresMemoryStore
from .orchestrator import AICodeOrchestrator
from .provider import OpenAICompatibleProvider
from .queue import PostgresTaskQueue
from .workers import AgentWorker

logger = logging.getLogger("rusa-agent-worker")


class LeaseLostError(RuntimeError):
    """Raised when the worker no longer owns the current task lease."""


class QueueWorker:
    def __init__(
        self,
        *,
        queue: PostgresTaskQueue,
        orchestrator: AICodeOrchestrator,
        memory: PostgresMemoryStore,
        poll_interval_seconds: float = 1.0,
        maintenance_interval_seconds: float = 3600.0,
        worker_id: str | None = None,
        lease_seconds: int = 300,
    ) -> None:
        if poll_interval_seconds <= 0 or maintenance_interval_seconds <= 0:
            raise ValueError("worker intervals must be positive")
        PostgresTaskQueue._validate_lease(lease_seconds)
        self.queue = queue
        self.orchestrator = orchestrator
        self.memory = memory
        self.poll_interval_seconds = poll_interval_seconds
        self.maintenance_interval_seconds = maintenance_interval_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = lease_seconds

    async def _run_with_heartbeat(
        self,
        task: CodingTask,
        checkpoint: TaskRecord | None,
        tenant_id: str,
        task_id: str,
    ) -> TaskRecord:
        async def persist(record: TaskRecord) -> None:
            saved = await asyncio.to_thread(
                self.queue.checkpoint,
                tenant_id,
                task_id,
                self.worker_id,
                record,
            )
            if not saved:
                raise LeaseLostError("task checkpoint rejected because the lease is no longer valid")

        execution = asyncio.create_task(
            self.orchestrator.run(task, checkpoint, checkpoint_callback=persist)
        )
        interval = max(5, self.lease_seconds // 3)
        while True:
            done, _ = await asyncio.wait({execution}, timeout=interval)
            if done:
                return await execution
            try:
                renewed = await asyncio.to_thread(
                    self.queue.heartbeat,
                    tenant_id,
                    task_id,
                    self.worker_id,
                    self.lease_seconds,
                )
            except Exception as exc:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise LeaseLostError("database error while renewing task lease") from exc
            if not renewed:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise LeaseLostError("task lease ownership was lost")

    async def run_once(self) -> bool:
        item = await asyncio.to_thread(
            self.queue.claim,
            self.worker_id,
            self.lease_seconds,
        )
        if item is None:
            return False
        tenant_id = item["tenant_id"]
        task_id = item["task_id"]
        try:
            task = CodingTask.from_dict(item["payload"])
            if task.tenant_id != tenant_id or task.task_id != task_id:
                raise ValueError("queued task identity does not match its database envelope")
            checkpoint = TaskRecord.from_dict(item["result"]) if item.get("result") else None
            record = await self._run_with_heartbeat(task, checkpoint, tenant_id, task_id)
            if record.state == TaskState.FAILED:
                saved = await asyncio.to_thread(
                    self.queue.retry,
                    tenant_id,
                    task_id,
                    "; ".join(record.errors) or "agent execution failed",
                    self.worker_id,
                )
                if not saved:
                    logger.warning("retry rejected after lease loss task_id=%s", task_id)
            else:
                await asyncio.to_thread(
                    self.queue.acknowledge,
                    tenant_id,
                    task_id,
                    record,
                    self.worker_id,
                )
            if record.state == TaskState.SUCCEEDED:
                summary = "\n".join(
                    f"{result.role.value}: {result.summary}"
                    for result in record.worker_results
                )
                await asyncio.to_thread(
                    self.memory.store,
                    tenant_id,
                    f"Objective: {task.objective}\nOutcome: {record.state.value}\n{summary}"[:7900],
                    source=f"task:{task_id}",
                    metadata={"task_id": task_id, "repository": task.repository.full_name},
                )
        except LeaseLostError:
            logger.warning("lease lost; task execution cancelled task_id=%s", task_id)
        except Exception:
            logger.exception("agent task processing failed task_id=%s", task_id)
            try:
                saved = await asyncio.to_thread(
                    self.queue.retry,
                    tenant_id,
                    task_id,
                    "worker processing failed",
                    self.worker_id,
                )
                if not saved:
                    logger.warning("failure retry rejected after lease loss task_id=%s", task_id)
            except Exception:
                logger.exception("could not persist worker failure task_id=%s", task_id)
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_maintenance = loop.time() + self.maintenance_interval_seconds
        while not stop_event.is_set():
            if loop.time() >= next_maintenance:
                try:
                    removed_tasks = await asyncio.to_thread(
                        self.queue.purge_terminal,
                        retention_days=30,
                    )
                    removed_memory = await asyncio.to_thread(self.memory.purge_expired)
                    logger.info(
                        "agent maintenance completed removed_tasks=%s removed_memory=%s",
                        removed_tasks,
                        removed_memory,
                    )
                except Exception:
                    logger.exception("agent maintenance failed")
                next_maintenance = loop.time() + self.maintenance_interval_seconds

            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass


def build_worker() -> QueueWorker:
    required = (
        "RUSA_AGENT_DATABASE_URL",
        "RUSA_AGENT_PROVIDER_BASE_URL",
        "RUSA_AGENT_PROVIDER_API_KEY",
        "RUSA_AGENT_MODEL",
        "RUSA_AGENT_GITHUB_TOKEN",
        "RUSA_AGENT_AUDIT_KEY",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("missing required environment variables: " + ", ".join(missing))
    dsn = os.environ["RUSA_AGENT_DATABASE_URL"]
    queue = PostgresTaskQueue(dsn)
    queue.initialize()
    memory = PostgresMemoryStore(dsn)
    memory.initialize()
    provider = OpenAICompatibleProvider(
        base_url=os.environ["RUSA_AGENT_PROVIDER_BASE_URL"],
        api_key=os.environ["RUSA_AGENT_PROVIDER_API_KEY"],
        model=os.environ["RUSA_AGENT_MODEL"],
        timeout_seconds=float(os.getenv("RUSA_AGENT_PROVIDER_TIMEOUT", "45")),
    )
    allowed_repositories = {
        item.strip().lower()
        for item in os.getenv("RUSA_AGENT_ALLOWED_REPOSITORIES", "StrategyG/RusaAi").split(",")
        if item.strip()
    }
    github = GitHubToolGateway(
        token=os.environ["RUSA_AGENT_GITHUB_TOKEN"],
        api_url=os.getenv("RUSA_AGENT_GITHUB_API_URL", "https://api.github.com"),
        allowed_repositories=allowed_repositories,
    )
    audit = HMACAuditLog(
        os.getenv("RUSA_AGENT_AUDIT_PATH", "/var/lib/rusa-agent/audit.jsonl"),
        os.environ["RUSA_AGENT_AUDIT_KEY"],
    )
    workers = [AgentWorker(role, provider, memory) for role in WorkerRole]
    orchestrator = AICodeOrchestrator(
        workers=workers,
        tools=github,
        audit=audit,
        worker_timeout_seconds=float(os.getenv("RUSA_AGENT_WORKER_TIMEOUT", "120")),
        max_repair_cycles=int(os.getenv("RUSA_AGENT_MAX_REPAIR_CYCLES", "2")),
    )
    return QueueWorker(
        queue=queue,
        orchestrator=orchestrator,
        memory=memory,
        poll_interval_seconds=float(os.getenv("RUSA_AGENT_POLL_INTERVAL", "1")),
        lease_seconds=int(os.getenv("RUSA_AGENT_LEASE_SECONDS", "300")),
    )


async def _serve() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = build_worker()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)
    await worker.run_forever(stop_event)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
