"""Worker process with lease renewal, durable checkpoints, and graceful shutdown."""

import asyncio
import json
import logging
import os
import signal
import socket
import uuid
from typing import Any

from .audit import HMACAuditLog
from .contracts import CodingTask, TaskRecord, TaskState, WorkerRole
from .github import GitHubToolGateway
from .memory import PostgresMemoryStore
from .mcp_gateway import LocalKnowledgeGateway, MCPKnowledgeGateway
from .rag_memory import CompositeMemoryReader
from .tool_router import CompositeToolGateway
from rusa_knowledge.service import KnowledgeService
from .orchestrator import AICodeOrchestrator, CheckpointPersistenceError
from .provider import OpenAICompatibleProvider
from .queue import PostgresTaskQueue
from .workers import AgentWorker
from rusa_observability.metrics import record_agent_queue_snapshot, start_metrics_server
from rusa_observability.telemetry import configure_telemetry

logger = logging.getLogger("rusa-agent-worker")


def _enabled_tool_set(knowledge_tools: set[str]) -> set[str]:
    from .mcp_gateway import KNOWLEDGE_TOOLS

    selected = set(knowledge_tools)
    if not selected.issubset(KNOWLEDGE_TOOLS):
        raise ValueError("configured knowledge tools contain an unapproved tool")
    return (set(AgentWorker.TOOL_NAMES) - set(KNOWLEDGE_TOOLS)) | selected


class LeaseLostError(RuntimeError):
    """Raised when the worker no longer owns the current task lease."""


def _load_repository_access() -> tuple[set[str], dict[str, set[str]]]:
    raw_global = os.getenv("RUSA_AGENT_ALLOWED_REPOSITORIES", "")
    global_repositories = {
        item.strip().lower() for item in raw_global.split(",") if item.strip()
    }
    if not global_repositories:
        raise RuntimeError("RUSA_AGENT_ALLOWED_REPOSITORIES must contain at least one repository")
    raw_tenants = os.getenv("RUSA_AGENT_TENANT_REPOSITORIES", "")
    try:
        tenant_values = json.loads(raw_tenants)
    except json.JSONDecodeError as exc:
        raise RuntimeError("RUSA_AGENT_TENANT_REPOSITORIES must be valid JSON") from exc
    if not isinstance(tenant_values, dict) or not tenant_values:
        raise RuntimeError("RUSA_AGENT_TENANT_REPOSITORIES must map tenants to repositories")
    tenant_repositories: dict[str, set[str]] = {}
    for tenant_id, values in tenant_values.items():
        if not isinstance(tenant_id, str) or not isinstance(values, list) or not values:
            raise RuntimeError("invalid tenant repository mapping")
        repositories = {
            item.strip().lower() for item in values
            if isinstance(item, str) and item.strip()
        }
        if len(repositories) != len(values) or not repositories.issubset(global_repositories):
            raise RuntimeError("tenant repositories must be a unique subset of the global allowlist")
        tenant_repositories[tenant_id] = repositories
    return global_repositories, tenant_repositories


def _load_required_ci_checks() -> tuple[str, ...]:
    raw = os.getenv("RUSA_AGENT_REQUIRED_CI_CHECKS", "")
    if not raw:
        return (
            "Lint, test, and audit dependencies",
            "Build and smoke-test containers",
        )
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("RUSA_AGENT_REQUIRED_CI_CHECKS must be a JSON array") from exc
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, str) and item.strip() for item in values)
        or len({item.strip() for item in values}) != len(values)
    ):
        raise RuntimeError("RUSA_AGENT_REQUIRED_CI_CHECKS must contain unique non-empty strings")
    return tuple(item.strip() for item in values)


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
        worker_id: str | None = None,
        lease_seconds: int = 300,
        tenant_ids: tuple[str, ...] = (),
        metrics_interval_seconds: float = 30.0,
    ) -> None:
        if (
            poll_interval_seconds <= 0
            or maintenance_interval_seconds <= 0
            or metrics_interval_seconds < 5
        ):
            raise ValueError("worker intervals must be positive and metrics interval at least 5s")
        PostgresTaskQueue._validate_lease(lease_seconds)
        self.queue = queue
        self.orchestrator = orchestrator
        self.memory = memory
        self.memory_reader = memory_reader or memory
        self.poll_interval_seconds = poll_interval_seconds
        self.maintenance_interval_seconds = maintenance_interval_seconds
        self.worker_id = worker_id or f"{socket.gethostname()[:80]}-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = lease_seconds
        self.tenant_ids = tuple(sorted(set(tenant_ids)))
        self.metrics_interval_seconds = metrics_interval_seconds

    def close(self) -> None:
        close = getattr(self.memory_reader, "close", None)
        if callable(close):
            close()

    async def _run_with_heartbeat(
        self,
        task: CodingTask,
        checkpoint: TaskRecord | None,
        tenant_id: str,
        task_id: str,
        lease_token: str,
    ) -> TaskRecord:
        async def persist(record: TaskRecord) -> None:
            saved = await asyncio.to_thread(
                self.queue.checkpoint,
                tenant_id,
                task_id,
                self.worker_id,
                lease_token,
                record,
            )
            if not saved:
                raise LeaseLostError(
                    "task checkpoint rejected because the lease is no longer valid"
                )

        async def tool_guard() -> bool:
            try:
                return await asyncio.to_thread(
                    self.queue.owns_lease,
                    tenant_id,
                    task_id,
                    self.worker_id,
                    lease_token,
                )
            except Exception:
                logger.exception("lease ownership check failed task_id=%s", task_id)
                return False

        execution = asyncio.create_task(
            self.orchestrator.run(
                task,
                checkpoint,
                checkpoint_callback=persist,
                tool_guard=tool_guard,
            )
        )
        interval = max(5, self.lease_seconds // 6)
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
                    lease_token,
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
        lease_token = item.get("lease_token")
        try:
            PostgresTaskQueue._validate_lease_token(lease_token)
        except ValueError:
            logger.error(
                "queue claim omitted a valid lease token; refusing task execution task_id=%s",
                task_id,
            )
            return True
        try:
            task = CodingTask.from_dict(item["payload"])
            if task.tenant_id != tenant_id or task.task_id != task_id:
                raise ValueError("queued task identity does not match its database envelope")
            checkpoint = TaskRecord.from_dict(item["result"]) if item.get("result") else None
            record = await self._run_with_heartbeat(
                task, checkpoint, tenant_id, task_id, lease_token,
            )
            if record.state == TaskState.FAILED:
                saved = await asyncio.to_thread(
                    self.queue.retry,
                    tenant_id,
                    task_id,
                    "; ".join(record.errors) or "agent execution failed",
                    self.worker_id,
                    lease_token,
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
                    lease_token,
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
        except CheckpointPersistenceError as exc:
            if isinstance(exc.__cause__, LeaseLostError):
                logger.warning(
                    "lease lost during checkpoint; task will be reclaimed task_id=%s",
                    task_id,
                )
            else:
                logger.exception("agent checkpoint persistence failed task_id=%s", task_id)
                try:
                    saved = await asyncio.to_thread(
                        self.queue.retry,
                        tenant_id,
                        task_id,
                        "worker checkpoint persistence failed",
                        self.worker_id,
                        lease_token,
                    )
                    if not saved:
                        logger.warning("checkpoint retry rejected after lease loss task_id=%s", task_id)
                except Exception:
                    logger.exception("checkpoint failure could not be rescheduled task_id=%s", task_id)
        except Exception:
            logger.exception("agent task processing failed task_id=%s", task_id)
            try:
                saved = await asyncio.to_thread(
                    self.queue.retry,
                    tenant_id,
                    task_id,
                    "worker processing failed",
                    self.worker_id,
                    lease_token,
                )
                if not saved:
                    logger.warning("failure retry rejected after lease loss task_id=%s", task_id)
            except Exception:
                logger.exception("could not persist worker failure task_id=%s", task_id)
        return True

    async def _refresh_queue_metrics(self) -> None:
        if not self.tenant_ids:
            return
        totals = {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "blocked": 0,
            "cancelled": 0,
            "waiting_external": 0,
        }
        oldest_queued_age = 0
        for tenant_id in self.tenant_ids:
            snapshot = await asyncio.to_thread(self.queue.stats, tenant_id)
            counts = snapshot.get("counts", {})
            for state in totals:
                totals[state] += max(0, int(counts.get(state, 0)))
            oldest_queued_age = max(
                oldest_queued_age,
                max(0, int(snapshot.get("oldest_queued_age_seconds", 0))),
            )
        record_agent_queue_snapshot(totals, oldest_queued_age)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_maintenance = loop.time() + self.maintenance_interval_seconds
        next_metrics = loop.time()
        while not stop_event.is_set():
            if self.tenant_ids and loop.time() >= next_metrics:
                try:
                    await self._refresh_queue_metrics()
                except Exception:
                    logger.exception("agent queue metrics refresh failed")
                next_metrics = loop.time() + self.metrics_interval_seconds
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

            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("agent queue poll failed; worker will retry")
                processed = False
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
        "RUSA_AGENT_ALLOWED_REPOSITORIES",
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
        max_response_bytes=int(os.getenv("RUSA_AGENT_PROVIDER_MAX_RESPONSE_BYTES", "2000000")),
    )
    allowed_repositories, tenant_repositories = _load_repository_access()
    github = GitHubToolGateway(
        token=os.environ["RUSA_AGENT_GITHUB_TOKEN"],
        api_url=os.getenv("RUSA_AGENT_GITHUB_API_URL", "https://api.github.com"),
        allowed_repositories=allowed_repositories,
        tenant_repositories=tenant_repositories,
        max_response_bytes=int(os.getenv("RUSA_AGENT_GITHUB_MAX_RESPONSE_BYTES", "4000000")),
        max_file_bytes=int(os.getenv("RUSA_AGENT_GITHUB_MAX_FILE_BYTES", "1000000")),
        max_diff_files=int(os.getenv("RUSA_AGENT_GITHUB_MAX_DIFF_FILES", "20")),
    )
    audit = HMACAuditLog(
        os.getenv("RUSA_AGENT_AUDIT_PATH", "/var/lib/rusa-agent/audit.jsonl"),
        os.environ["RUSA_AGENT_AUDIT_KEY"],
    )
    rag_enabled = os.getenv("RUSA_RAG_ENABLED", "false").lower() == "true"
    memory_reader = memory
    tool_gateway = github
    knowledge_service = None
    knowledge_tools: set[str] = set()
    if rag_enabled:
        knowledge_service = KnowledgeService.from_env(check_ready=True)
        memory_reader = CompositeMemoryReader(memory, knowledge_service)
        mcp_server_url = os.getenv("RUSA_MCP_SERVER_URL", "").strip()
        if mcp_server_url:
            try:
                tenant_tokens = json.loads(os.getenv("RUSA_MCP_TENANT_TOKENS", "{}"))
            except json.JSONDecodeError as exc:
                raise RuntimeError("RUSA_MCP_TENANT_TOKENS must be valid JSON") from exc
            if not isinstance(tenant_tokens, dict):
                raise RuntimeError("RUSA_MCP_TENANT_TOKENS must map tenant IDs to bearer tokens")
            allowed_tools = {
                item.strip() for item in os.getenv(
                    "RUSA_MCP_ALLOWED_TOOLS",
                    "search_knowledge,get_document,list_collections",
                ).split(",") if item.strip()
            }
            knowledge_tools = allowed_tools
            allowed_hosts = {
                item.strip() for item in os.getenv("RUSA_MCP_INTERNAL_HTTP_HOSTS", "rusa-mcp").split(",")
                if item.strip()
            }
            knowledge_gateway = MCPKnowledgeGateway(
                server_url=mcp_server_url,
                tenant_tokens=tenant_tokens,
                allowed_tools=allowed_tools,
                allow_http_hosts=allowed_hosts,
            )
        else:
            knowledge_tools = {
                item.strip() for item in os.getenv(
                    "RUSA_MCP_ALLOWED_TOOLS",
                    "search_knowledge,get_document,list_collections",
                ).split(",") if item.strip()
            }
            knowledge_gateway = LocalKnowledgeGateway(
                knowledge_service,
                default_collection=os.getenv("RUSA_KNOWLEDGE_COLLECTION", "agent"),
            )
        tool_gateway = CompositeToolGateway(github, knowledge_gateway)
    enabled_tools = _enabled_tool_set(knowledge_tools)
    workers = [
        AgentWorker(role, provider, memory_reader, enabled_tools=enabled_tools)
        for role in WorkerRole
    ]
    orchestrator = AICodeOrchestrator(
        workers=workers,
        tools=tool_gateway,
        audit=audit,
        worker_timeout_seconds=float(os.getenv("RUSA_AGENT_WORKER_TIMEOUT", "120")),
        max_repair_cycles=int(os.getenv("RUSA_AGENT_MAX_REPAIR_CYCLES", "2")),
        required_ci_checks=_load_required_ci_checks(),
    )
    return QueueWorker(
        queue=queue,
        orchestrator=orchestrator,
        memory=memory,
        memory_reader=memory_reader,
        poll_interval_seconds=float(os.getenv("RUSA_AGENT_POLL_INTERVAL", "1")),
        lease_seconds=int(os.getenv("RUSA_AGENT_LEASE_SECONDS", "300")),
        tenant_ids=tuple(tenant_repositories),
        metrics_interval_seconds=float(os.getenv("RUSA_AGENT_METRICS_INTERVAL_SECONDS", "30")),
    )


async def _serve() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    configure_telemetry("rusa-agent-worker")
    if os.getenv("RUSA_METRICS_ENABLED", "false").lower() == "true":
        start_metrics_server(
            port=int(os.getenv("RUSA_WORKER_METRICS_PORT", "9100")),
            addr="0.0.0.0",
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
