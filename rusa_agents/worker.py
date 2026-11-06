"""Worker process entrypoint for the PostgreSQL-backed agent runtime."""

import asyncio
import logging
import json
import re
import os
import signal
from typing import Any

from .audit import HMACAuditLog
from .contracts import WorkerRole, TaskState
from .github import GitHubToolGateway
from .memory import PostgresMemoryStore
from .rag_memory import CompositeMemoryReader
from .mcp_gateway import MCPKnowledgeGateway
from .tool_router import CompositeToolGateway
from rusa_knowledge.service import KnowledgeService
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
        memory_reader: Any | None = None,
        poll_interval_seconds: float = 1.0,
        maintenance_interval_seconds: float = 3600.0,
        lease_seconds: int = 300,
        heartbeat_interval_seconds: float = 60.0,
    ) -> None:
        if (
            poll_interval_seconds <= 0 or maintenance_interval_seconds <= 0
            or not 10 <= lease_seconds <= 3600
            or not 0 < heartbeat_interval_seconds < lease_seconds
        ):
            raise ValueError("worker intervals or lease settings are invalid")
        self.queue = queue
        self.orchestrator = orchestrator
        self.memory = memory
        self.memory_reader = memory_reader or memory
        self.poll_interval_seconds = poll_interval_seconds
        self.maintenance_interval_seconds = maintenance_interval_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def close(self) -> None:
        close = getattr(self.memory_reader, "close", None)
        if callable(close):
            close()

    async def _run_with_lease(self, task, checkpoint, tenant_id, task_id, lease_token):
        stop_event = asyncio.Event()

        async def maintain_lease():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.heartbeat_interval_seconds,
                    )
                    return
                except asyncio.TimeoutError:
                    renewed = await asyncio.to_thread(
                        self.queue.renew_lease,
                        tenant_id,
                        task_id,
                        lease_token,
                        self.lease_seconds,
                    )
                    if not renewed:
                        raise RuntimeError("worker lease was lost; execution must stop")

        async def persist_checkpoint(record):
            saved = await asyncio.to_thread(
                self.queue.checkpoint,
                tenant_id,
                task_id,
                lease_token,
                record,
            )
            if not saved:
                raise RuntimeError("task checkpoint rejected because the lease is no longer valid")

        process = asyncio.create_task(
            self.orchestrator.run(
                task,
                checkpoint,
                checkpoint_callback=persist_checkpoint,
            )
        )
        heartbeat = asyncio.create_task(maintain_lease())
        try:
            done, _ = await asyncio.wait(
                {process, heartbeat}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                error = heartbeat.exception()
                if error is not None:
                    process.cancel()
                    await asyncio.gather(process, return_exceptions=True)
                    raise RuntimeError("worker lease was lost; task execution was cancelled") from error
            return await process
        finally:
            stop_event.set()
            if not heartbeat.done():
                await heartbeat

    async def run_once(self) -> bool:
        try:
            item = await asyncio.to_thread(self.queue.claim, self.lease_seconds)
        except Exception:
            logger.exception("agent queue claim failed")
            return False
        if item is None:
            return False
        tenant_id = item["tenant_id"]
        task_id = item["task_id"]
        lease_token = item["lease_token"]
        task = None
        try:
            from .contracts import CodingTask, TaskRecord

            task = CodingTask.from_dict(item["payload"])
            if task.tenant_id != tenant_id or task.task_id != task_id:
                raise ValueError("queued task identity does not match its tenant-scoped row")
            checkpoint = TaskRecord.from_dict(item["result"]) if item.get("result") else None
            record = await self._run_with_lease(
                task, checkpoint, tenant_id, task_id, lease_token,
            )
            if record.state == TaskState.FAILED:
                await asyncio.to_thread(
                    self.queue.retry, tenant_id, task_id, "; ".join(record.errors), lease_token,
                )
            else:
                await asyncio.to_thread(
                    self.queue.acknowledge, tenant_id, task_id, record, lease_token,
                )
        except Exception:
            logger.exception("agent task processing failed task_id=%s", task_id)
            try:
                await asyncio.to_thread(
                    self.queue.retry, tenant_id, task_id, "worker processing failed", lease_token,
                )
            except Exception:
                logger.exception("task could not be rescheduled because its lease is no longer owned")
            return True

        if record.state == TaskState.SUCCEEDED:
            try:
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
                logger.exception("task succeeded but episodic memory persistence failed task_id=%s", task_id)
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
        "RUSA_AGENT_TENANT_REPOSITORIES",
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
    try:
        tenant_repositories = json.loads(os.environ["RUSA_AGENT_TENANT_REPOSITORIES"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("RUSA_AGENT_TENANT_REPOSITORIES must be valid JSON") from exc
    if (
        not isinstance(tenant_repositories, dict)
        or not tenant_repositories
        or any(
            not isinstance(tenant_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tenant_id)
            or not isinstance(repositories, list)
            or not repositories
            or any(
                not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                for repository in repositories
            )
            or len(set(repositories)) != len(repositories)
            for tenant_id, repositories in tenant_repositories.items()
        )
    ):
        raise RuntimeError("RUSA_AGENT_TENANT_REPOSITORIES is invalid")
    github = GitHubToolGateway(
        token=os.environ["RUSA_AGENT_GITHUB_TOKEN"],
        api_url=os.getenv("RUSA_AGENT_GITHUB_API_URL", "https://api.github.com"),
        allowed_repositories_by_tenant={
            tenant_id: set(repositories)
            for tenant_id, repositories in tenant_repositories.items()
        },
    )
    audit = HMACAuditLog(
        os.getenv("RUSA_AGENT_AUDIT_PATH", "/var/lib/rusa-agent/audit.jsonl"),
        os.environ["RUSA_AGENT_AUDIT_KEY"],
    )

    memory_reader: Any = memory
    rag_enabled = os.getenv("RUSA_RAG_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    if rag_enabled:
        knowledge_service = KnowledgeService.from_env(check_ready=True)
        memory_reader = CompositeMemoryReader(
            memory,
            knowledge_service,
            collection=os.getenv("RUSA_KNOWLEDGE_COLLECTION", "agent"),
        )

    tools: Any = github
    mcp_server_url = os.getenv("RUSA_MCP_SERVER_URL", "").strip()
    tenant_tokens_raw = os.getenv("RUSA_MCP_TENANT_TOKENS", "").strip()
    if mcp_server_url:
        try:
            tenant_tokens = json.loads(tenant_tokens_raw) if tenant_tokens_raw else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("RUSA_MCP_TENANT_TOKENS must be valid JSON") from exc
        if not isinstance(tenant_tokens, dict) or not tenant_tokens:
            raise RuntimeError("RUSA_MCP_TENANT_TOKENS must map tenant IDs to bearer tokens")
        allowed_tools = {
            item.strip()
            for item in os.getenv(
                "RUSA_MCP_ALLOWED_TOOLS",
                "search_knowledge,get_document,list_collections",
            ).split(",")
            if item.strip()
        }
        unauthorized_tenants = set(tenant_tokens) - set(tenant_repositories)
        if unauthorized_tenants:
            raise RuntimeError("RUSA_MCP_TENANT_TOKENS contains tenants outside the repository allowlist")
        internal_http_hosts = {
            item.strip().lower()
            for item in os.getenv("RUSA_MCP_INTERNAL_HTTP_HOSTS", "").split(",")
            if item.strip()
        }
        mcp_gateway = MCPKnowledgeGateway(
            server_url=mcp_server_url,
            tenant_tokens=tenant_tokens,
            allowed_tools=allowed_tools,
            allow_http_hosts=internal_http_hosts,
        )
        tools = CompositeToolGateway(github, mcp_gateway)
    elif tenant_tokens_raw:
        raise RuntimeError("RUSA_MCP_TENANT_TOKENS requires RUSA_MCP_SERVER_URL")

    workers = [AgentWorker(role, provider, memory_reader) for role in WorkerRole]
    orchestrator = AICodeOrchestrator(
        workers=workers,
        tools=tools,
        audit=audit,
        worker_timeout_seconds=float(os.getenv("RUSA_AGENT_WORKER_TIMEOUT", "120")),
        max_repair_cycles=int(os.getenv("RUSA_AGENT_MAX_REPAIR_CYCLES", "2")),
    )
    return QueueWorker(
        queue=queue,
        orchestrator=orchestrator,
        memory=memory,
        memory_reader=memory_reader,
        poll_interval_seconds=float(os.getenv("RUSA_AGENT_POLL_INTERVAL", "1")),
        lease_seconds=int(os.getenv("RUSA_AGENT_QUEUE_LEASE_SECONDS", "300")),
        heartbeat_interval_seconds=float(os.getenv("RUSA_AGENT_QUEUE_HEARTBEAT_SECONDS", "60")),
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
    try:
        await worker.run_forever(stop_event)
    finally:
        worker.close()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
