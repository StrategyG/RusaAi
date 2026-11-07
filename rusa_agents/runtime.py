"""Production worker runtime wiring PostgreSQL, model provider, Git worktrees, and sandbox."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
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
from .tool_gateway import DockerSandbox, WorkspaceToolGateway

logger = logging.getLogger("rusa-ai.aicode.runtime")


class TaskCancellationRequested(RuntimeError):
    """Raised when the durable task control plane requests cooperative cancellation."""


class AICodeRuntime:
    def __init__(self, config: RuntimeConfig | None = None, worker_id: str | None = None):
        self.config = config or RuntimeConfig.from_env()
        self.config.validate_worker()
        self._pending_operations: set[asyncio.Task[Any]] = set()
        if not self.config.worker_database_url or not self.config.workspace_root or not self.config.git_mirror_root:
            raise RuntimeError("Database, workspace root, and Git mirror root are required")
        if self.config.environment == "production" and not os.getenv("DOCKER_HOST"):
            raise RuntimeError("Production worker requires DOCKER_HOST connected to a dedicated rootless sandbox daemon")
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
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


    async def shutdown(self, timeout_seconds: float = 10.0) -> None:
        """Cancel and drain tracked child operations before releasing worker resources."""
        pending = set(self._pending_operations)
        for task in pending:
            task.cancel()
        if not pending:
            return
        done, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
        for task in done:
            self._pending_operations.discard(task)
            if not task.cancelled():
                task.exception()
        if still_pending:
            logger.error(
                "%d child operation(s) remained active after %.1fs shutdown grace",
                len(still_pending),
                timeout_seconds,
            )

    def close(self) -> None:
        """Release optional retrieval resources during worker shutdown."""
        if self.knowledge_service is not None:
            self.knowledge_service.close()
            self.knowledge_service = None

    def initialize(self) -> None:
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
        tenant_suffix = hashlib.sha256(job.tenant_id.encode("utf-8")).hexdigest()[:12]
        lease_suffix = job.lease_token.replace("-", "")[:12]
        return CodingTask(
            objective=data["objective"],
            repository=RepositoryTarget(
                full_name=repository["full_name"],
                base_branch=repository.get("base_branch", "main"),
                work_branch=f"feature/aicode/{tenant_suffix}-{job.task_id}-{lease_suffix}",
            ),
            acceptance_criteria=data["acceptance_criteria"],
            context=data.get("context", ""),
            task_id=job.task_id,
            metadata={
                "tenant_id": job.tenant_id,
                "lease_token": job.lease_token,
                "publish_branch": f"feature/aicode/{tenant_suffix}-{job.task_id}",
            },
        )

    async def _build_orchestrator(
        self,
        task: CodingTask,
        tenant_id: str,
        job: Job,
        lost_lease: asyncio.Event,
        cancel_requested: asyncio.Event,
    ) -> tuple[AICodeOrchestrator, str]:
        workspace = await asyncio.to_thread(
            self.workspace_manager.create, task, tenant_id
        )
        def execution_guard() -> bool:
            if lost_lease.is_set() or cancel_requested.is_set():
                return False
            return self.queue.owns_lease(
                job.id, job.tenant_id, self.worker_id, job.lease_token
            )

        gateway = WorkspaceToolGateway(
            workspace,
            sandbox=self.sandbox,
            knowledge_service=self.knowledge_service,
            knowledge_tenant_id=tenant_id if self.knowledge_service is not None else None,
            knowledge_collection=os.getenv("RUSA_KNOWLEDGE_COLLECTION", "agent"),
            execution_guard=execution_guard,
        )
        workers = [
            AutonomousCodeWorker(role, self.provider, gateway)
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

    @staticmethod
    async def _cancel_child(task: asyncio.Task, *, timeout_seconds: float = 10.0) -> None:
        """Propagate cancellation and wait briefly for cooperative child cleanup."""
        if task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Child operation failed while cancellation was being propagated")
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.error(
                "Child operation did not stop within %.1fs after cancellation",
                timeout_seconds,
            )
        except Exception:
            logger.exception("Child operation raised while cancellation was being propagated")

    async def _await_with_lease(
        self,
        operation: Coroutine,
        lost_lease: asyncio.Event,
        cancel_requested: asyncio.Event | None = None,
    ) -> Any:
        """Race work against lease loss and durable cancellation without orphaning asyncio tasks."""
        operation_task = asyncio.create_task(operation)
        pending_operations = getattr(self, "_pending_operations", None)
        if pending_operations is None:
            pending_operations = set()
            self._pending_operations = pending_operations
        pending_operations.add(operation_task)

        def release_operation(task: asyncio.Task) -> None:
            pending_operations.discard(task)
            if not task.cancelled():
                task.exception()

        operation_task.add_done_callback(release_operation)
        lease_watch = asyncio.create_task(lost_lease.wait())
        cancel_watch = (
            asyncio.create_task(cancel_requested.wait())
            if cancel_requested is not None else None
        )
        watchers = [lease_watch] + ([cancel_watch] if cancel_watch is not None else [])
        try:
            await asyncio.wait(
                {operation_task, *watchers},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_requested is not None and cancel_requested.is_set():
                await self._cancel_child(operation_task)
                raise TaskCancellationRequested("Task cancellation was requested")
            if lost_lease.is_set():
                await self._cancel_child(operation_task)
                raise LeaseLostError("Worker lost its task lease; operation cancelled")
            return await operation_task
        except asyncio.CancelledError:
            await self._cancel_child(operation_task)
            raise
        finally:
            for watcher in watchers:
                watcher.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)

    async def _heartbeat(
        self,
        job: Job,
        lost_lease: asyncio.Event,
        cancel_requested: asyncio.Event,
    ) -> None:
        try:
            interval = float(os.getenv("RUSA_HEARTBEAT_INTERVAL_SECONDS", "10"))
        except ValueError:
            logger.exception("Invalid RUSA_HEARTBEAT_INTERVAL_SECONDS")
            lost_lease.set()
            return
        if not 2 <= interval <= 60:
            logger.error("RUSA_HEARTBEAT_INTERVAL_SECONDS must be between 2 and 60")
            lost_lease.set()
            return
        while True:
            await asyncio.sleep(interval)
            for attempt in range(3):
                try:
                    if await asyncio.to_thread(
                        self.queue.is_cancel_requested, job.task_id, job.tenant_id
                    ):
                        cancel_requested.set()
                        logger.info("Cancellation requested for task %s", job.task_id)
                        return
                    renewed = await asyncio.to_thread(
                        self.queue.heartbeat,
                        job.id,
                        job.tenant_id,
                        self.worker_id,
                        job.lease_token,
                        180,
                    )
                    if renewed:
                        break
                    if await asyncio.to_thread(
                        self.queue.is_cancel_requested, job.task_id, job.tenant_id
                    ):
                        cancel_requested.set()
                        logger.info("Cancellation requested for task %s", job.task_id)
                    else:
                        lost_lease.set()
                        logger.error("Lease lost for job %s", job.id)
                    return
                except Exception:
                    if attempt == 2:
                        logger.exception(
                            "Lease heartbeat failed after bounded retries for job %s",
                            job.id,
                        )
                        lost_lease.set()
                        return
                    await asyncio.sleep(min(2 ** attempt, 4))

    async def _assert_lease(
        self,
        job: Job,
        lost_lease: asyncio.Event,
        cancel_requested: asyncio.Event | None = None,
    ) -> None:
        if cancel_requested is not None and cancel_requested.is_set():
            raise TaskCancellationRequested("Task cancellation was requested")
        if lost_lease.is_set():
            raise LeaseLostError("Worker has already lost its task lease")
        if await asyncio.to_thread(
            self.queue.is_cancel_requested, job.task_id, job.tenant_id
        ):
            if cancel_requested is not None:
                cancel_requested.set()
            raise TaskCancellationRequested("Task cancellation was requested")
        owned = await asyncio.to_thread(
            self.queue.owns_lease,
            job.id,
            job.tenant_id,
            self.worker_id,
            job.lease_token,
        )
        if not owned:
            if await asyncio.to_thread(
                self.queue.is_cancel_requested, job.task_id, job.tenant_id
            ):
                if cancel_requested is not None:
                    cancel_requested.set()
                raise TaskCancellationRequested("Task cancellation was requested")
            lost_lease.set()
            raise LeaseLostError("Task lease is no longer owned by this worker")

    @staticmethod
    def _retry_delay_seconds(attempt: int) -> int:
        """Full-jitter exponential delay, capped to five minutes."""
        ceiling = min(300, 2 ** max(0, attempt - 1))
        return random.randint(0, ceiling)

    @staticmethod
    def _is_retryable_failure(exc: Exception) -> bool:
        """Classify deterministic client/policy errors separately from transient failures."""
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            if status_code == 403:
                headers = getattr(response, "headers", None) or {}
                return str(headers.get("X-RateLimit-Remaining", "")) == "0"
            return status_code in {408, 425, 429, 500, 502, 503, 504}
        return not isinstance(exc, (PermissionError, ValueError, TypeError))

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
            if await asyncio.to_thread(
                self.queue.is_cancel_requested, job.task_id, job.tenant_id
            ):
                raise TaskCancellationRequested("Task cancellation was requested")
            raise LeaseLostError(
                "Queue rejected awaiting-approval transition because the lease was lost"
            )
        await asyncio.to_thread(
            self.audit.record,
            "task.awaiting_approval",
            task.task_id,
            self.worker_id,
            {"reason": reason[:2000]},
            tenant_id=job.tenant_id,
        )

    async def _acknowledge_cancellation(
        self,
        job: Job,
        *,
        publication_started: bool,
        publication_finished: bool,
        pr_creation_started: bool,
        pr_creation_finished: bool,
    ) -> None:
        def side_effect_state(started: bool, finished: bool) -> str:
            if not started:
                return "not_started"
            return "completed" if finished else "reconciliation_required"

        side_effects = {
            "git_publication": side_effect_state(
                publication_started, publication_finished
            ),
            "pull_request_creation": side_effect_state(
                pr_creation_started, pr_creation_finished
            ),
        }
        try:
            marked = await asyncio.to_thread(
                self.queue.mark_cancelled,
                job.id,
                job.tenant_id,
                self.worker_id,
                job.lease_token,
                {"side_effects": side_effects},
            )
            if marked:
                await asyncio.to_thread(
                    self.audit.record,
                    "task.cancelled",
                    job.task_id,
                    self.worker_id,
                    {"side_effects": side_effects},
                    tenant_id=job.tenant_id,
                )
            else:
                logger.warning(
                    "Cancellation was requested but worker could not acknowledge task %s",
                    job.task_id,
                )
        except Exception:
            logger.exception(
                "Could not persist cancellation acknowledgement for task %s",
                job.task_id,
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
        cancel_requested = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(job, lost_lease, cancel_requested)
        )
        publication_started = False
        publication_finished = False
        pr_creation_started = False
        pr_creation_finished = False
        authorized = False
        policy_audited = False
        try:
            task = self._task(job)
            allowed_repositories = self.config.tenant_repositories.get(job.tenant_id)
            if allowed_repositories is not None and task.repository.full_name not in allowed_repositories:
                raise PermissionError("Tenant is not authorized for this repository")
            if self.config.environment == "production" and not allowed_repositories:
                raise PermissionError("Tenant has no configured repository permissions")

            await self._assert_lease(job, lost_lease, cancel_requested)
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
                if await asyncio.to_thread(
                    self.queue.is_cancel_requested, job.task_id, job.tenant_id
                ):
                    cancel_requested.set()
                    raise TaskCancellationRequested("Task was cancelled before execution")
                lost_lease.set()
                raise LeaseLostError("Worker is not authorized by the current task lease")
            orchestrator, workspace = await self._build_orchestrator(
                task, job.tenant_id, job, lost_lease, cancel_requested
            )
            await self._assert_lease(job, lost_lease, cancel_requested)
            task_timeout = int(os.getenv("RUSA_TASK_TIMEOUT_SECONDS", "3600"))
            if not 60 <= task_timeout <= 86400:
                raise ValueError("RUSA_TASK_TIMEOUT_SECONDS must be between 60 and 86400")
            record = await self._await_with_lease(
                asyncio.wait_for(orchestrator.run(task), timeout=task_timeout),
                lost_lease,
                cancel_requested,
            )
            await self._assert_lease(job, lost_lease, cancel_requested)
            if record.state == TaskState.AWAITING_APPROVAL:
                await self._await_human_review(
                    job,
                    task,
                    "; ".join(record.errors) or "Human intervention required",
                    {
                        "branch": task.repository.work_branch,
                        "publish_branch": task.metadata["publish_branch"],
                        "published": False,
                    },
                )
                outcome = "awaiting_approval"
                return True
            if record.state != TaskState.SUCCEEDED:
                retryable = record.state == TaskState.FAILED
                failed = await asyncio.to_thread(
                    self.queue.fail,
                    job.id,
                    job.tenant_id,
                    self.worker_id,
                    job.lease_token,
                    "; ".join(record.errors) or f"Task ended in {record.state.value}",
                    self._retry_delay_seconds(job.attempts),
                    retryable=retryable,
                )
                if not failed:
                    await self._assert_lease(job, lost_lease, cancel_requested)
                    raise LeaseLostError(
                        "Queue rejected the failure transition because the lease changed"
                    )
                outcome = "failed" if retryable else "blocked"
                return True

            await self._assert_lease(job, lost_lease, cancel_requested)

            def publish_if_not_cancelled() -> str | None:
                if self.queue.is_cancel_requested(job.task_id, job.tenant_id):
                    raise TaskCancellationRequested(
                        "Cancellation was requested before Git publication"
                    )
                return self.publisher.commit_and_push(
                    workspace,
                    task.repository.full_name,
                    task.repository.work_branch,
                    f"feat(aicode): {task.objective[:120]}",
                    publish_branch=task.metadata["publish_branch"],
                )

            publication_started = True
            commit_sha = await self._await_with_lease(
                asyncio.to_thread(
                    self.queue.publish_if_lease_owned,
                    job.id,
                    job.tenant_id,
                    self.worker_id,
                    job.lease_token,
                    publish_if_not_cancelled,
                    lease_seconds=900,
                ),
                lost_lease,
                cancel_requested,
            )
            publication_finished = True
            await self._assert_lease(job, lost_lease, cancel_requested)
            pr_creation_started = True
            pr_url = await self._await_with_lease(
                self.publisher.create_or_get_draft_pr(
                    task.repository.full_name,
                    task.metadata["publish_branch"],
                    task.repository.base_branch,
                    f"AICode: {task.objective[:180]}",
                    (
                        "## Autonomous implementation\n\n"
                        f"Task ID: {task.task_id}\n"
                        f"RUSA-AI-Task-ID: {task.task_id}\n\n"
                        f"Workspace commit: {commit_sha}\n\n"
                        "Acceptance criteria:\n" +
                        "\n".join(f"- {item}" for item in task.acceptance_criteria) +
                        "\n\nAutomated test and security evidence is recorded in the task audit trail. "
                        "This pull request is draft and requires human review before merge."
                    ),
                ),
                lost_lease,
                cancel_requested,
            )
            pr_creation_finished = True
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
                cancel_requested,
            )
            if not ci_ok:
                await self._await_human_review(
                    job,
                    task,
                    ci_reason,
                    {
                        "pull_request_url": pr_url,
                        "commit_sha": commit_sha,
                        "branch": task.metadata["publish_branch"],
                    },
                )
                outcome = "awaiting_approval"
                return True

            await self._assert_lease(job, lost_lease, cancel_requested)
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
                    "branch": task.metadata["publish_branch"],
                    "workspace_branch": task.repository.work_branch,
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
        except TaskCancellationRequested:
            outcome = "cancelled"
            await self._acknowledge_cancellation(
                job,
                publication_started=publication_started,
                publication_finished=publication_finished,
                pr_creation_started=pr_creation_started,
                pr_creation_finished=pr_creation_finished,
            )
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
            if not cancel_requested.is_set():
                try:
                    if await asyncio.to_thread(
                        self.queue.is_cancel_requested, job.task_id, job.tenant_id
                    ):
                        cancel_requested.set()
                except Exception:
                    logger.warning(
                        "Could not refresh cancellation state while handling task %s",
                        job.task_id,
                    )
            if cancel_requested.is_set():
                outcome = "cancelled"
                await self._acknowledge_cancellation(
                    job,
                    publication_started=publication_started,
                    publication_finished=publication_finished,
                    pr_creation_started=pr_creation_started,
                    pr_creation_finished=pr_creation_finished,
                )
            else:
                logger.exception("AICode task %s failed", job.task_id)
                outcome = "lease_lost" if lost_lease.is_set() else "failed"
                if not lost_lease.is_set():
                    retryable = self._is_retryable_failure(exc)
                    try:
                        failed = await asyncio.to_thread(
                            self.queue.fail,
                            job.id,
                            job.tenant_id,
                            self.worker_id,
                            job.lease_token,
                            f"{type(exc).__name__}: {exc}",
                            self._retry_delay_seconds(job.attempts),
                            retryable=retryable,
                        )
                    except Exception:
                        logger.exception(
                            "Could not persist failure transition for task %s",
                            job.task_id,
                        )
                        lost_lease.set()
                        outcome = "lease_lost"
                    else:
                        if not failed:
                            try:
                                if await asyncio.to_thread(
                                    self.queue.is_cancel_requested,
                                    job.task_id,
                                    job.tenant_id,
                                ):
                                    cancel_requested.set()
                                    outcome = "cancelled"
                                    await self._acknowledge_cancellation(
                                        job,
                                        publication_started=publication_started,
                                        publication_finished=publication_finished,
                                        pr_creation_started=pr_creation_started,
                                        pr_creation_finished=pr_creation_finished,
                                    )
                                else:
                                    lost_lease.set()
                                    outcome = "lease_lost"
                            except Exception:
                                logger.exception(
                                    "Could not reconcile rejected failure transition for task %s",
                                    job.task_id,
                                )
                                lost_lease.set()
                                outcome = "lease_lost"
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Heartbeat task failed while cleaning up task %s", job.task_id)
            AICODE_TASKS.labels(outcome).inc()
            AICODE_TASK_DURATION.labels(outcome).observe(time.perf_counter() - started)
        return True

    async def run_forever(
        self, poll_seconds: float = 2.0, stop_event: asyncio.Event | None = None
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        consecutive_errors = 0
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_errors += 1
                delay = random.uniform(
                    0,
                    min(30, 2 ** min(consecutive_errors - 1, 5)),
                )
                logger.exception(
                    "Worker polling failed; retrying in %.2fs (failure %d)",
                    delay,
                    consecutive_errors,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                continue
            if not processed:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
