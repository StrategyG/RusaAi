"""Production worker runtime wiring PostgreSQL, model provider, Git worktrees, and sandbox."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
from collections.abc import Coroutine
from typing import Any

from .audit import PostgresAuditLog, PostgresTaskPolicyAudit, record_task_policy
from .autonomous import AutonomousCodeWorker
from .config import RuntimeConfig
from .contracts import CodingTask, RepositoryTarget, TaskState
from .git_workspace import GitHubPublisher, GitWorkspaceManager
from .memory import PostgresMemory
from .metrics import AICODE_TASK_DURATION, AICODE_TASKS
from .orchestrator import AICodeOrchestrator
from .providers import OpenAICompatibleProvider
from .queue import Job, LeaseLostError, PostgresTaskQueue
from .runtime_v1 import AgentRuntimeV1
from .tool_gateway import DockerSandbox, WorkspaceToolGateway
from .worker_execution import (
    LeaseBoundCheckpointStore,
    PolicyAuthorizer,
    ResultVerifier,
    TaskMemoryCheckpointStore,
    WorkerExecutionEngine,
)

logger = logging.getLogger("rusa-ai.aicode.runtime")


class AICodeRuntime:
    def __init__(self, config: RuntimeConfig | None = None, worker_id: str | None = None):
        self.config = config or RuntimeConfig.from_env()
        self.config.validate_worker()
        if not self.config.worker_database_url or not self.config.workspace_root or not self.config.git_mirror_root:
            raise RuntimeError("Database, workspace root, and Git mirror root are required")
        if self.config.environment == "production" and not os.getenv("DOCKER_HOST"):
            raise RuntimeError("Production worker requires DOCKER_HOST connected to a dedicated rootless sandbox daemon")
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.agent_runtime: AgentRuntimeV1 | None = None
        self.queue = PostgresTaskQueue(self.config.worker_database_url)
        self.memory = PostgresMemory(self.config.worker_database_url)
        if not self.config.audit_hmac_key:
            raise RuntimeError("RUSA_AUDIT_HMAC_KEY is required by the worker runtime")
        self.audit = PostgresAuditLog(self.config.worker_database_url, self.config.audit_hmac_key)
        self.task_policy_audit = PostgresTaskPolicyAudit(self.config.worker_database_url)
        self.workspace_manager = GitWorkspaceManager(
            self.config.git_mirror_root,
            self.config.workspace_root,
            github_token=self.config.github_token,
        )
        self.provider = OpenAICompatibleProvider()
        self.publisher = GitHubPublisher(self.config.github_token)
        self.sandbox = DockerSandbox(image=self.config.sandbox_image)
        rag_flag = os.getenv("RUSA_RAG_ENABLED", "false").strip().lower()
        if rag_flag not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise RuntimeError("RUSA_RAG_ENABLED must be a boolean value")
        self.knowledge_service = None
        if rag_flag in {"1", "true", "yes", "on"}:
            from rusa_knowledge.service import KnowledgeService

            knowledge_dsn = os.getenv("RUSA_KNOWLEDGE_DATABASE_URL") or self.config.worker_database_url
            self.knowledge_service = KnowledgeService.from_env(
                check_ready=True,
                database_url=knowledge_dsn,
            )


    def close(self) -> None:
        """Release optional retrieval resources during worker shutdown."""
        if self.knowledge_service is not None:
            self.knowledge_service.close()
            self.knowledge_service = None

    def _load_agent_runtime_registry(self) -> dict[str, Any]:
        """Load the v1 registry and verify the existing six-worker AICode binding."""
        runtime = AgentRuntimeV1()
        compatibility = runtime.validate_aicode_worker_bindings(
            role.value for role in AICodeOrchestrator.PIPELINE
        )
        self.agent_runtime = runtime
        logger.info(
            "Agent Runtime v1 registry loaded: status=%s roles=%s dispatchable=%s",
            runtime.status()["status"],
            runtime.status()["role_count"],
            compatibility["dispatchable"],
        )
        return compatibility

    def build_agent_execution_engine(
        self,
        job: Job,
        policy_authorizer: PolicyAuthorizer,
        result_verifier: ResultVerifier,
    ) -> WorkerExecutionEngine:
        """Compose the staged DAG engine with tenant memory and the live queue lease."""
        if job.lease_owner != self.worker_id:
            raise PermissionError("Agent Runtime job lease belongs to a different worker")
        if not job.tenant_id or not job.lease_token:
            raise ValueError("Agent Runtime job requires a tenant and lease token")
        if self.agent_runtime is None:
            self._load_agent_runtime_registry()
        if self.agent_runtime is None:
            raise RuntimeError("Agent Runtime registry failed to initialize")

        checkpoint_store = LeaseBoundCheckpointStore(
            TaskMemoryCheckpointStore(self.memory),
            self.queue,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
        )
        return self.agent_runtime.build_execution_engine(
            checkpoint_store,
            policy_authorizer,
            result_verifier,
            worker_parallelism=1,
        )

    def initialize(self) -> None:
        self._load_agent_runtime_registry()
        self.queue.initialize()
        self.memory.initialize()
        self.audit.initialize()
        if self.config.environment == "production":
            self.task_policy_audit.verify_ready("rusa_worker_runtime")
        self.sandbox.verify_ready(
            require_rootless=self.config.environment == "production",
            require_image=self.config.environment == "production",
        )
        if not self.audit.verify():
            raise RuntimeError("Central audit ledger integrity verification failed")

    @staticmethod
    def _task(job: Job) -> CodingTask:
        data: dict[str, Any] = job.payload
        repository = data["repository"]
        lease_suffix = job.lease_token.replace("-", "")[:8]
        return CodingTask(
            objective=data["objective"],
            repository=RepositoryTarget(
                full_name=repository["full_name"],
                base_branch=repository.get("base_branch", "main"),
                work_branch=f"feature/aicode/{job.task_id[:12]}-{lease_suffix}",
            ),
            acceptance_criteria=data["acceptance_criteria"],
            context=data.get("context", ""),
            task_id=job.task_id,
            metadata={"tenant_id": job.tenant_id, "lease_token": job.lease_token},
        )

    @staticmethod
    def _knowledge_project_scope(repository_full_name: str) -> str:
        """Derive a stable project scope from the already-authorized repository target."""
        if not isinstance(repository_full_name, str) or repository_full_name.count("/") != 1:
            raise ValueError("repository target cannot be mapped to a knowledge project scope")
        project_id = repository_full_name.lower().replace("/", "--")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project_id):
            raise ValueError("repository target contains unsupported project-scope characters")
        return project_id

    async def _build_orchestrator(
        self, task: CodingTask, tenant_id: str
    ) -> tuple[AICodeOrchestrator, str]:
        tenant_provider_factory = getattr(self.provider, "for_tenant", None)
        tenant_provider = (
            tenant_provider_factory(tenant_id)
            if callable(tenant_provider_factory)
            else self.provider
        )
        workspace = await asyncio.to_thread(
            self.workspace_manager.create, task, tenant_id
        )
        gateway = WorkspaceToolGateway(
            workspace,
            sandbox=self.sandbox,
            knowledge_service=self.knowledge_service,
            knowledge_tenant_id=tenant_id if self.knowledge_service is not None else None,
            knowledge_collection=os.getenv("RUSA_KNOWLEDGE_COLLECTION", "agent"),
            knowledge_project_id=(
                self._knowledge_project_scope(task.repository.full_name)
                if self.knowledge_service is not None else None
            ),
        )
        workers = [
            AutonomousCodeWorker(role, tenant_provider, gateway)
            for role in AICodeOrchestrator.PIPELINE
        ]
        return (
            AICodeOrchestrator(
                workers=workers,
                memory=self.memory,
                audit=self.audit,
                max_parallel_workers=int(os.getenv("RUSA_MAX_PARALLEL_WORKERS", "4")),
                max_repair_cycles=int(os.getenv("RUSA_MAX_REPAIR_CYCLES", "2")),
            ),
            str(workspace),
        )

    async def _await_with_lease(
        self, operation: Coroutine, lost_lease: asyncio.Event
    ) -> Any:
        operation_task = asyncio.create_task(operation)
        lease_watch = asyncio.create_task(lost_lease.wait())
        try:
            done, _ = await asyncio.wait(
                {operation_task, lease_watch},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_watch in done and lost_lease.is_set():
                operation_task.cancel()
                try:
                    await operation_task
                except asyncio.CancelledError:
                    pass
                raise RuntimeError("Worker lost its task lease; operation cancelled")
            return await operation_task
        finally:
            lease_watch.cancel()
            try:
                await lease_watch
            except asyncio.CancelledError:
                pass

    async def _heartbeat(self, job: Job, lost_lease: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                renewed = await asyncio.to_thread(
                    self.queue.heartbeat, job.id, job.tenant_id, self.worker_id, job.lease_token, 180
                )
            except Exception:
                logger.exception("Lease heartbeat failed for job %s", job.id)
                lost_lease.set()
                return
            if not renewed:
                lost_lease.set()
                logger.error("Lease lost for job %s", job.id)
                return

    async def _assert_lease(self, job: Job, lost_lease: asyncio.Event) -> None:
        if lost_lease.is_set():
            raise RuntimeError("Worker has already lost its task lease")
        owned = await asyncio.to_thread(
            self.queue.owns_lease,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
        )
        if not owned:
            lost_lease.set()
            raise RuntimeError("Task lease is no longer owned by this worker")

    async def _await_human_review(
        self,
        job: Job,
        task: CodingTask,
        reason: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        updated = await asyncio.to_thread(
            self.queue.await_approval,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
            reason,
            result,
        )
        if not updated:
            raise RuntimeError("Queue rejected awaiting-approval transition because the lease was lost")
        await asyncio.to_thread(
            self.audit.record,
            "task.awaiting_approval",
            task.task_id,
            self.worker_id,
            {"reason": reason[:2000]},
            tenant_id=job.tenant_id,
        )

    async def run_once(self) -> bool:
        if not self.config.aicode_enabled:
            return False
        job = await asyncio.to_thread(self.queue.claim, self.worker_id, 180)
        if job is None:
            return False
        started = time.perf_counter()
        outcome = "failed"
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job, lost_lease))
        authorized = False
        policy_audited = False
        try:
            task = self._task(job)
            allowed_repositories = self.config.tenant_repositories.get(job.tenant_id)
            if allowed_repositories is not None and task.repository.full_name not in allowed_repositories:
                raise PermissionError("Tenant is not authorized for this repository")
            if self.config.environment == "production" and not allowed_repositories:
                raise PermissionError("Tenant has no configured repository permissions")

            await self._assert_lease(job, lost_lease)
            authorized = await asyncio.to_thread(
                self.queue.authorize_execution,
                job.id,
                task.task_id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
            )
            policy_audited = True
            record_task_policy(
                job.tenant_id,
                job.task_id,
                f"worker:{self.worker_id}",
                "task.execute",
                authorized,
            )
            if not authorized:
                lost_lease.set()
                raise RuntimeError("Worker is not authorized by the current task lease")
            orchestrator, workspace = await self._build_orchestrator(task, job.tenant_id)
            if lost_lease.is_set():
                raise RuntimeError("Worker lost its task lease before execution")
            task_timeout = int(os.getenv("RUSA_TASK_TIMEOUT_SECONDS", "3600"))
            if not 60 <= task_timeout <= 86400:
                raise ValueError("RUSA_TASK_TIMEOUT_SECONDS must be between 60 and 86400")
            record = await self._await_with_lease(
                asyncio.wait_for(orchestrator.run(task), timeout=task_timeout),
                lost_lease,
            )
            if record.state == TaskState.AWAITING_APPROVAL:
                await self._await_human_review(
                    job,
                    task,
                    "; ".join(record.errors) or "Human intervention required",
                    {"branch": task.repository.work_branch},
                )
                outcome = "awaiting_approval"
                return True
            if record.state != TaskState.SUCCEEDED:
                await asyncio.to_thread(
                    self.queue.fail, job.id, job.tenant_id, self.worker_id, job.lease_token,
                    "; ".join(record.errors) or f"Task ended in {record.state.value}",
                )
                outcome = "failed"
                return True

            await self._assert_lease(job, lost_lease)
            commit_sha = await asyncio.to_thread(
                self.queue.publish_if_lease_owned,
                job.id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
                lambda: self.publisher.commit_and_push(
                    workspace,
                    task.repository.full_name,
                    task.repository.work_branch,
                    f"feat(aicode): {task.objective[:120]}",
                ),
                lease_seconds=900,
            )
            await self._assert_lease(job, lost_lease)
            pr_url = await self._await_with_lease(
                self.publisher.create_or_get_draft_pr(
                    task.repository.full_name,
                    task.repository.work_branch,
                    task.repository.base_branch,
                    f"AICode: {task.objective[:180]}",
                    (
                        "## Autonomous implementation\n\n"
                        f"Task ID: {task.task_id}\n\n"
                        f"Workspace commit: {commit_sha}\n\n"
                        "Acceptance criteria:\n" +
                        "\n".join(f"- {item}" for item in task.acceptance_criteria) +
                        "\n\nAutomated test and security evidence is recorded in the task audit trail. "
                        "This pull request is draft and requires human review before merge."
                    ),
                ),
                lost_lease,
            )
            await asyncio.to_thread(
                self.audit.record,
                "pull_request.created",
                task.task_id,
                self.worker_id,
                {"pull_request_url": pr_url, "commit_sha": commit_sha},
                tenant_id=job.tenant_id,
            )
            ci_timeout = int(os.getenv("RUSA_CI_TIMEOUT_SECONDS", "900"))
            ci_poll = int(os.getenv("RUSA_CI_POLL_SECONDS", "15"))
            if not 60 <= ci_timeout <= 3600 or not 5 <= ci_poll <= 60:
                raise ValueError("CI timeout must be 60-3600s and poll interval 5-60s")
            ci_ok, ci_reason = await self._await_with_lease(
                self.publisher.wait_for_ci(
                    task.repository.full_name, commit_sha,
                    timeout_seconds=ci_timeout, poll_seconds=ci_poll,
                ),
                lost_lease,
            )
            if not ci_ok:
                await self._await_human_review(
                    job,
                    task,
                    ci_reason,
                    {
                        "pull_request_url": pr_url,
                        "commit_sha": commit_sha,
                        "branch": task.repository.work_branch,
                    },
                )
                outcome = "awaiting_approval"
                return True

            await asyncio.to_thread(
                self.audit.record,
                "ci.verified",
                task.task_id,
                self.worker_id,
                {"commit_sha": commit_sha, "pull_request_url": pr_url, "result": ci_reason},
                tenant_id=job.tenant_id,
            )
            completed = await asyncio.to_thread(
                self.queue.complete,
                job.id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
                {
                    "pull_request_url": pr_url,
                    "commit_sha": commit_sha,
                    "branch": task.repository.work_branch,
                },
            )
            if not completed:
                raise RuntimeError("Queue rejected completion because the lease is no longer owned")
            try:
                await asyncio.to_thread(self.workspace_manager.cleanup, task, job.tenant_id)
            except Exception:
                logger.exception("Could not clean successful task workspace %s", task.task_id)
            logger.info("AICode task %s completed: %s", task.task_id, pr_url)
            outcome = "success"
        except Exception as exc:
            if isinstance(exc, LeaseLostError):
                lost_lease.set()
            if not policy_audited:
                try:
                    record_task_policy(
                        job.tenant_id,
                        job.task_id,
                        f"worker:{self.worker_id}",
                        "task.execute",
                        False,
                        sink=self.task_policy_audit,
                        required=self.config.environment == "production",
                    )
                except Exception:
                    logger.error("Could not persist denied task execution audit event")
            elif not authorized:
                record_task_policy(
                    job.tenant_id, job.task_id, f"worker:{self.worker_id}",
                    "task.execute", False,
                )
            logger.exception("AICode task %s failed", job.task_id)
            outcome = "lease_lost" if lost_lease.is_set() else "failed"
            if not lost_lease.is_set():
                await asyncio.to_thread(
                    self.queue.fail, job.id, job.tenant_id, self.worker_id, job.lease_token,
                    f"{type(exc).__name__}: {exc}",
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            AICODE_TASKS.labels(outcome).inc()
            AICODE_TASK_DURATION.labels(outcome).observe(time.perf_counter() - started)
        return True

    async def run_forever(
        self, poll_seconds: float = 2.0, stop_event: asyncio.Event | None = None
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
