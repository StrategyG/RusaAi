"""Production worker runtime wiring PostgreSQL, model provider, Git worktrees, and sandbox."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from .audit import PostgresAuditLog
from .autonomous import AutonomousCodeWorker
from .config import RuntimeConfig
from .contracts import CodingTask, RepositoryTarget, TaskState
from .git_workspace import GitHubPublisher, GitWorkspaceManager
from .memory import PostgresMemory
from .orchestrator import AICodeOrchestrator
from .providers import OpenAICompatibleProvider
from .queue import Job, PostgresTaskQueue
from .tool_gateway import DockerSandbox, WorkspaceToolGateway

logger = logging.getLogger("rusa-ai.aicode.runtime")


class AICodeRuntime:
    def __init__(self, config: RuntimeConfig | None = None, worker_id: str | None = None):
        self.config = config or RuntimeConfig.from_env()
        self.config.validate()
        if not self.config.database_url or not self.config.workspace_root or not self.config.git_mirror_root:
            raise RuntimeError("Database, workspace root, and Git mirror root are required")
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.queue = PostgresTaskQueue(self.config.database_url)
        self.memory = PostgresMemory(self.config.database_url)
        if not self.config.audit_hmac_key:
            raise RuntimeError("RUSA_AUDIT_HMAC_KEY is required by the worker runtime")
        self.audit = PostgresAuditLog(self.config.database_url, self.config.audit_hmac_key)
        self.workspace_manager = GitWorkspaceManager(
            self.config.git_mirror_root, self.config.workspace_root
        )
        self.provider = OpenAICompatibleProvider()
        self.publisher = GitHubPublisher(self.config.github_token)

    def initialize(self) -> None:
        self.queue.initialize()
        self.memory.initialize()
        self.audit.initialize()
        if not self.audit.verify():
            raise RuntimeError("Central audit ledger integrity verification failed")

    @staticmethod
    def _task(job: Job) -> CodingTask:
        data: dict[str, Any] = job.payload
        repository = data["repository"]
        return CodingTask(
            objective=data["objective"],
            repository=RepositoryTarget(
                full_name=repository["full_name"],
                base_branch=repository.get("base_branch", "main"),
                work_branch=repository["work_branch"],
            ),
            acceptance_criteria=data["acceptance_criteria"],
            context=data.get("context", ""),
            task_id=job.task_id,
            metadata={"tenant_id": job.tenant_id},
        )

    async def _build_orchestrator(self, task: CodingTask, tenant_id: str) -> tuple[AICodeOrchestrator, str]:
        workspace = self.workspace_manager.create(task, tenant_id)
        gateway = WorkspaceToolGateway(
            workspace,
            sandbox=DockerSandbox(image=self.config.sandbox_image),
        )
        workers = [
            AutonomousCodeWorker(role, self.provider, gateway)
            for role in AICodeOrchestrator.PIPELINE
        ]
        return (
            AICodeOrchestrator(workers=workers, memory=self.memory, audit=self.audit),
            str(workspace),
        )

    async def _heartbeat(self, job: Job, lost_lease: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                renewed = await asyncio.to_thread(
                    self.queue.heartbeat, job.id, job.tenant_id, self.worker_id, 180
                )
            except Exception:
                logger.exception("Lease heartbeat failed for job %s", job.id)
                lost_lease.set()
                return
            if not renewed:
                lost_lease.set()
                logger.error("Lease lost for job %s", job.id)
                return

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(self.queue.claim, self.worker_id, 180)
        if job is None:
            return False
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job, lost_lease))
        try:
            task = self._task(job)
            allowed_repositories = self.config.tenant_repositories.get(job.tenant_id)
            if allowed_repositories is not None and task.repository.full_name not in allowed_repositories:
                raise PermissionError("Tenant is not authorized for this repository")
            if self.config.environment == "production" and not allowed_repositories:
                raise PermissionError("Tenant has no configured repository permissions")
            orchestrator, workspace = await self._build_orchestrator(task, job.tenant_id)
            task_timeout = int(os.getenv("RUSA_TASK_TIMEOUT_SECONDS", "3600"))
            record = await asyncio.wait_for(orchestrator.run(task), timeout=task_timeout)
            if lost_lease.is_set():
                raise RuntimeError("Worker lost its task lease; refusing to publish")
            if record.state != TaskState.SUCCEEDED:
                await asyncio.to_thread(
                    self.queue.fail, job.id, job.tenant_id, self.worker_id,
                    "; ".join(record.errors) or f"Task ended in {record.state.value}",
                )
                return True

            commit_sha = await asyncio.to_thread(
                self.publisher.commit_and_push,
                workspace, task.repository.full_name,
                task.repository.work_branch,
                f"feat(aicode): {task.objective[:120]}",
            )
            if lost_lease.is_set():
                raise RuntimeError("Worker lost its task lease before PR creation")
            pr_url = await self.publisher.create_or_get_draft_pr(
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
            )
            self.audit.record(
                "pull_request.created", task.task_id, self.worker_id,
                {"pull_request_url": pr_url, "commit_sha": commit_sha},
                tenant_id=job.tenant_id,
            )
            completed = await asyncio.to_thread(
                self.queue.complete, job.id, job.tenant_id, self.worker_id
            )
            if not completed:
                raise RuntimeError("Queue rejected completion because the lease is no longer owned")
            logger.info("AICode task %s completed: %s", task.task_id, pr_url)
        except Exception as exc:
            logger.exception("AICode task %s failed", job.task_id)
            if not lost_lease.is_set():
                await asyncio.to_thread(
                    self.queue.fail, job.id, job.tenant_id, self.worker_id,
                    f"{type(exc).__name__}: {exc}",
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def run_forever(self, poll_seconds: float = 2.0) -> None:
        while True:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(poll_seconds)
