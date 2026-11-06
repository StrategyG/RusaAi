"""Worker process entrypoint for the PostgreSQL-backed agent runtime."""

import asyncio
import logging
import os
import signal

from .audit import HMACAuditLog
from .contracts import WorkerRole, TaskState
from .github import GitHubToolGateway
from .memory import PostgresMemoryStore
from .orchestrator import AICodeOrchestrator
from .provider import OpenAICompatibleProvider
from .queue import PostgresTaskQueue
from .workers import AgentWorker

logger = logging.getLogger("rusa-agent-worker")


class QueueWorker:
    def __init__(
        self,
        *,
        queue: PostgresTaskQueue,
        orchestrator: AICodeOrchestrator,
        memory: PostgresMemoryStore,
        poll_interval_seconds: float = 1.0,
        maintenance_interval_seconds: float = 3600.0,
    ) -> None:
        if poll_interval_seconds <= 0 or maintenance_interval_seconds <= 0:
            raise ValueError("worker intervals must be positive")
        self.queue = queue
        self.orchestrator = orchestrator
        self.memory = memory
        self.poll_interval_seconds = poll_interval_seconds
        self.maintenance_interval_seconds = maintenance_interval_seconds

    async def run_once(self) -> bool:
        item = self.queue.claim()
        if item is None:
            return False
        tenant_id = item["tenant_id"]
        task_id = item["task_id"]
        try:
            from .contracts import CodingTask, TaskRecord

            task = CodingTask.from_dict(item["payload"])
            checkpoint = TaskRecord.from_dict(item["result"]) if item.get("result") else None
            record = await self.orchestrator.run(task, checkpoint)
            if record.state == TaskState.FAILED:
                self.queue.retry(tenant_id, task_id, "; ".join(record.errors))
            else:
                self.queue.acknowledge(tenant_id, task_id, record)
            if record.state == TaskState.SUCCEEDED:
                summary = "\n".join(
                    f"{result.role.value}: {result.summary}"
                    for result in record.worker_results
                )
                self.memory.store(
                    tenant_id,
                    f"Objective: {task.objective}\nOutcome: {record.state.value}\n{summary}"[:7900],
                    source=f"task:{task_id}",
                    metadata={"task_id": task_id, "repository": task.repository.full_name},
                )
        except Exception:
            logger.exception("agent task processing failed task_id=%s", task_id)
            self.queue.retry(tenant_id, task_id, "worker processing failed")
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_maintenance = loop.time() + self.maintenance_interval_seconds
        while not stop_event.is_set():
            if loop.time() >= next_maintenance:
                try:
                    removed_tasks = self.queue.purge_terminal(retention_days=30)
                    removed_memory = self.memory.purge_expired()
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
    github = GitHubToolGateway(
        token=os.environ["RUSA_AGENT_GITHUB_TOKEN"],
        api_url=os.getenv("RUSA_AGENT_GITHUB_API_URL", "https://api.github.com"),
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
