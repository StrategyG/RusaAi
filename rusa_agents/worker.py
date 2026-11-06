"""Durable worker execution with lease recovery and evidence-gated completion."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .audit import AuditLog
from .contracts import TaskStatus, WorkerMessage, WorkerRole, validate_transition
from .messaging import AgentMessageBus
from .orchestrator import HierarchicalOrchestrator
from .policy import AutonomyPolicy
from .security import redact_sensitive_data
from .providers import OpenAICompatibleProvider
from .storage import Database, TaskRow, utcnow
from .tools import ToolError, ToolRegistry

logger = logging.getLogger("rusa-agent-worker")


class LeaseLost(RuntimeError):
    """Raised when a worker no longer owns a task lease."""


class IdempotencyConflict(ValueError):
    """Raised when an idempotency key is reused with a different request."""


class AgentRuntime:
    def __init__(
        self,
        database: Database,
        provider: OpenAICompatibleProvider,
        tools: ToolRegistry,
        policy: AutonomyPolicy | None = None,
        max_concurrency: int = 4,
        lease_seconds: int = 300,
        heartbeat_seconds: int = 30,
    ):
        if max_concurrency < 1 or lease_seconds < 60 or heartbeat_seconds < 5:
            raise ValueError("Invalid worker concurrency or lease configuration")
        self.database = database
        self.provider = provider
        self.tools = tools
        self.policy = policy or AutonomyPolicy()
        self.audit = AuditLog(database)
        self.messages = AgentMessageBus(database)
        self.orchestrator = HierarchicalOrchestrator(provider, tools)
        self.worker_id = f"worker-{uuid4().hex[:12]}"
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    @staticmethod
    def _set_status(row: TaskRow, target: TaskStatus) -> None:
        validate_transition(row.status, target)
        row.status = target.value

    async def _publish(
        self,
        tenant_id: str,
        task_id: str,
        from_agent: WorkerRole,
        to_agent: WorkerRole,
        message_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        message = WorkerMessage(
            task_id=task_id,
            correlation_id=task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            message_type=message_type,
            payload=payload or {},
        )
        await self.messages.publish(tenant_id, message)

    async def submit(self, tenant_id: str, idempotency_key: str, payload: Any) -> str:
        from .contracts import TaskCreate

        if not tenant_id or len(tenant_id) > 128:
            raise ValueError("Invalid tenant identifier")
        if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
            raise ValueError("Idempotency key must contain 8 to 128 characters")
        task = TaskCreate.model_validate(payload)
        safe_objective = redact_sensitive_data(task.objective)
        safe_context = redact_sensitive_data(task.context)
        safe_acceptance_criteria = redact_sensitive_data(task.acceptance_criteria)
        safe_payload = task.model_dump(mode="json")
        safe_payload["objective"] = safe_objective
        safe_payload["context"] = safe_context
        safe_payload["acceptance_criteria"] = safe_acceptance_criteria
        request_hash = hashlib.sha256(
            json.dumps(safe_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        async with self.database.session() as session:
            existing = await session.scalar(
                select(TaskRow).where(
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.idempotency_key == idempotency_key,
                )
            )
            if existing:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used with a different task payload"
                    )
                return existing.id
            row = TaskRow(
                id=str(uuid4()),
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                objective=safe_objective,
                repository=task.repository,
                base_branch=task.base_branch,
                work_branch=task.work_branch,
                context=safe_context,
                acceptance_criteria=safe_acceptance_criteria,
                allowed_tools=task.allowed_tools,
                max_attempts=task.max_attempts,
                require_ci=task.require_ci,
                require_pull_request=task.require_pull_request,
                status=TaskStatus.QUEUED.value,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(TaskRow).where(
                        TaskRow.tenant_id == tenant_id,
                        TaskRow.idempotency_key == idempotency_key,
                    )
                )
                if existing:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key was already used with a different task payload"
                        )
                    return existing.id
                raise
            task_id = row.id
        await self.audit.record(
            tenant_id, task_id, "api", "task.queued", {"repository": task.repository}
        )
        await self._publish(
            tenant_id,
            task_id,
            WorkerRole.SUPERVISOR,
            WorkerRole.ANALYST,
            "task.accepted",
            {"objective": safe_objective, "repository": task.repository},
        )
        return task_id

    async def get_task(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.id == task_id,
                )
            )
            return self._serialize(row) if row else None

    async def approve(self, tenant_id: str, task_id: str, action: str) -> None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.id == task_id,
                )
            )
            if (
                not row
                or row.status != TaskStatus.AWAITING_APPROVAL.value
                or row.pending_action != action
            ):
                raise ValueError("No matching approval request exists")
            row.approved_actions = list(dict.fromkeys([*(row.approved_actions or []), action]))
            row.pending_action = None
            self._set_status(row, TaskStatus.QUEUED)
            row.available_at = utcnow()
            await session.commit()
        await self.audit.record(
            tenant_id, task_id, "human", "task.action_approved", {"action": action}
        )

    async def resume(self, tenant_id: str, task_id: str) -> None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.id == task_id,
                )
            )
            if not row or row.status != TaskStatus.BLOCKED.value or not row.plan:
                raise ValueError("Only a blocked task with a persisted plan can be resumed")
            if row.attempts >= row.max_attempts:
                raise ValueError("Task retry budget is exhausted; create a new task after review")
            error = row.error or ""
            if "CI checks" in error:
                target_tool = "github.get_ci_status"
            elif "pull request" in error:
                target_tool = "github.create_pull_request"
            else:
                raise ValueError("This blocker requires operator review and cannot be auto-resumed")
            indexes = [
                index for index, step in enumerate(row.plan.get("steps", []))
                if step.get("tool") == target_tool
            ]
            if not indexes:
                raise ValueError("The persisted plan has no step for the blocked operation")
            resume_cursor = indexes[-1]
            row.cursor = resume_cursor
            row.tool_results = list(row.tool_results or [])[:resume_cursor]
            row.error = None
            self._set_status(row, TaskStatus.QUEUED)
            row.available_at = utcnow()
            await session.commit()
        await self.audit.record(
            tenant_id,
            task_id,
            "human",
            "task.resumed",
            {"resume_from": target_tool, "cursor": resume_cursor},
        )
        await self._publish(
            tenant_id,
            task_id,
            WorkerRole.SUPERVISOR,
            WorkerRole.TESTER if target_tool == "github.get_ci_status" else WorkerRole.DEVELOPER,
            "task.accepted",
            {"resumed_from": target_tool},
        )

    async def cancel(self, tenant_id: str, task_id: str) -> None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.id == task_id,
                )
            )
            if not row:
                raise KeyError("Task not found")
            if row.status in {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELLED.value}:
                return
            row.cancel_requested = True
            if row.status in {
                TaskStatus.QUEUED.value,
                TaskStatus.AWAITING_APPROVAL.value,
                TaskStatus.BLOCKED.value,
            }:
                self._set_status(row, TaskStatus.CANCELLED)
                row.lease_owner = None
                row.lease_until = None
            await session.commit()
        await self.audit.record(tenant_id, task_id, "api", "task.cancel_requested")

    async def recover_expired_leases(self) -> int:
        recovered: list[tuple[str, str, str]] = []
        now = utcnow()
        async with self.database.session() as session:
            rows = (
                await session.execute(
                    select(TaskRow)
                    .where(
                        TaskRow.status == TaskStatus.RUNNING.value,
                        TaskRow.lease_until.is_not(None),
                        TaskRow.lease_until < now,
                    )
                    .order_by(TaskRow.created_at)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            for row in rows:
                if row.cancel_requested:
                    target = TaskStatus.CANCELLED
                elif row.attempts < row.max_attempts:
                    target = TaskStatus.QUEUED
                else:
                    target = TaskStatus.FAILED
                self._set_status(row, target)
                row.error = "Worker lease expired; task recovered by scheduler"
                row.lease_owner = None
                row.lease_until = None
                row.available_at = now
                recovered.append((row.tenant_id, row.id, target.value))
            await session.commit()
        for tenant_id, task_id, state in recovered:
            await self.audit.record(
                tenant_id, task_id, self.worker_id, "task.lease_recovered", {"status": state}
            )
        return len(recovered)

    async def claim(self) -> str | None:
        await self.recover_expired_leases()
        async with self.database.session() as session:
            statement = (
                select(TaskRow)
                .where(
                    TaskRow.status == TaskStatus.QUEUED.value,
                    TaskRow.available_at <= utcnow(),
                )
                .order_by(TaskRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if not row:
                return None
            self._set_status(row, TaskStatus.RUNNING)
            row.attempts += 1
            row.lease_owner = self.worker_id
            row.lease_until = utcnow() + timedelta(seconds=self.lease_seconds)
            await session.commit()
            return row.id

    async def _heartbeat(self, task_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
                return
            except asyncio.TimeoutError:
                pass
            try:
                async with self.database.session() as session:
                    result = await session.execute(
                        update(TaskRow)
                        .where(
                            TaskRow.id == task_id,
                            TaskRow.lease_owner == self.worker_id,
                            TaskRow.status == TaskStatus.RUNNING.value,
                        )
                        .values(
                            lease_until=utcnow() + timedelta(seconds=self.lease_seconds)
                        )
                    )
                    await session.commit()
                    if result.rowcount != 1:
                        logger.warning("Lease lost task_id=%s", task_id)
                        return
            except Exception:
                logger.exception("Lease heartbeat failed task_id=%s", task_id)

    async def run_once(self) -> bool:
        task_id = await self.claim()
        if not task_id:
            return False
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(task_id, stop))
        async with self.semaphore:
            try:
                await self.execute(task_id)
            except LeaseLost:
                logger.warning("Worker stopped after losing lease task_id=%s", task_id)
            except Exception:
                logger.exception("Worker failed task_id=%s", task_id)
                await self._fail(
                    task_id,
                    "Worker execution failed; inspect correlated audit events",
                )
            finally:
                stop.set()
                await heartbeat
        return True

    async def _assert_lease(self, tenant_id: str, task_id: str) -> None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.id == task_id,
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.status == TaskStatus.RUNNING.value,
                    TaskRow.lease_owner == self.worker_id,
                )
            )
            if row is None:
                raise LeaseLost(f"Worker no longer owns task {task_id}")

    async def execute(self, task_id: str) -> None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.id == task_id,
                    TaskRow.lease_owner == self.worker_id,
                )
            )
            if not row:
                raise LeaseLost(f"Worker does not own task {task_id}")
            snapshot = self._serialize(row)
        tenant_id = snapshot["tenant_id"]
        await self.audit.record(tenant_id, task_id, self.worker_id, "task.started")
        await self._publish(
            tenant_id,
            task_id,
            WorkerRole.SUPERVISOR,
            WorkerRole.ANALYST,
            "task.progress",
            {"status": "planning"},
        )

        if not snapshot["plan"]:
            plan = await self.orchestrator.create_plan(snapshot)
            await self._assert_lease(tenant_id, task_id)
            async with self.database.session() as session:
                row = await session.scalar(
                    select(TaskRow).where(
                        TaskRow.id == task_id,
                        TaskRow.tenant_id == tenant_id,
                        TaskRow.lease_owner == self.worker_id,
                    )
                )
                if not row:
                    raise LeaseLost(f"Worker no longer owns task {task_id}")
                row.plan = plan.model_dump(mode="json")
                row.cursor = 0
                await session.commit()
            snapshot["plan"] = plan.model_dump(mode="json")

        steps = snapshot["plan"]["steps"]
        while True:
            async with self.database.session() as session:
                row = await session.scalar(
                    select(TaskRow).where(
                        TaskRow.id == task_id,
                        TaskRow.tenant_id == tenant_id,
                        TaskRow.lease_owner == self.worker_id,
                    )
                )
                if not row:
                    raise LeaseLost(f"Worker no longer owns task {task_id}")
                if row.cancel_requested:
                    self._set_status(row, TaskStatus.CANCELLED)
                    row.lease_owner = None
                    row.lease_until = None
                    await session.commit()
                    await self.audit.record(
                        tenant_id, task_id, self.worker_id, "task.cancelled"
                    )
                    return
                cursor = row.cursor
                previous = list(row.tool_results or [])
                approved = set(row.approved_actions or [])
                allowed = set(row.allowed_tools or [])
            if cursor >= len(steps):
                break

            step = steps[cursor]
            tool_name = step["tool"]
            decision = self.policy.evaluate(tool_name, allowed, approved)
            if not decision.allowed:
                if decision.requires_approval:
                    async with self.database.session() as session:
                        row = await session.scalar(
                            select(TaskRow).where(
                                TaskRow.id == task_id,
                                TaskRow.tenant_id == tenant_id,
                                TaskRow.lease_owner == self.worker_id,
                            )
                        )
                        if not row:
                            raise LeaseLost(f"Worker no longer owns task {task_id}")
                        self._set_status(row, TaskStatus.AWAITING_APPROVAL)
                        row.pending_action = tool_name
                        row.lease_owner = None
                        row.lease_until = None
                        await session.commit()
                    await self.audit.record(
                        tenant_id,
                        task_id,
                        self.worker_id,
                        "task.awaiting_approval",
                        {"action": tool_name},
                    )
                    await self._publish(
                        tenant_id,
                        task_id,
                        WorkerRole.SUPERVISOR,
                        WorkerRole.SECURITY,
                        "task.blocked",
                        {"reason": "human_approval_required", "action": tool_name},
                    )
                    return
                raise ToolError(decision.reason)

            arguments = dict(step["arguments"])
            if tool_name == "rag.search":
                arguments["tenant_id"] = tenant_id
            if tool_name.startswith("github."):
                if arguments.get("repository") != snapshot["repository"]:
                    raise ToolError("Tool repository must match the task repository")
                if tool_name == "github.create_branch":
                    if (
                        arguments.get("base_branch") != snapshot["base_branch"]
                        or arguments.get("work_branch") != snapshot["work_branch"]
                    ):
                        raise ToolError("Branch creation must use the task's exact branches")
                if tool_name == "github.write_file":
                    if arguments.get("branch") != snapshot["work_branch"]:
                        raise ToolError("Writes are restricted to the task's exact work branch")
                if tool_name == "github.compare_branches":
                    if (
                        arguments.get("head") != snapshot["work_branch"]
                        or arguments.get("base") != snapshot["base_branch"]
                    ):
                        raise ToolError("Branch comparison must use the task's exact branches")
                if tool_name == "github.create_pull_request":
                    if (
                        arguments.get("head") != snapshot["work_branch"]
                        or arguments.get("base") != snapshot["base_branch"]
                    ):
                        raise ToolError("Pull request branches must match the task scope")
                if tool_name == "github.get_ci_status":
                    writes = [
                        item for item in previous
                        if item["tool"] == "github.write_file"
                    ]
                    if not writes:
                        raise ToolError("CI verification requires a preceding repository write")
                    latest_sha = writes[-1]["result"].get("commit_sha")
                    if not latest_sha:
                        raise ToolError("Latest write did not return a commit SHA")
                    if arguments.get("commit_sha") != "$latest_write_commit":
                        raise ToolError("CI verification must use the latest write commit placeholder")
                    arguments["commit_sha"] = latest_sha

            await self._assert_lease(tenant_id, task_id)
            if tool_name == "agent.review_diff":
                comparisons = [
                    item for item in previous
                    if item["tool"] == "github.compare_branches"
                ]
                if not comparisons:
                    raise ToolError("Reviewer requires a persisted branch comparison")
                result = await self.orchestrator.review_diff(
                    snapshot,
                    comparisons[-1]["result"],
                )
                if not result["approved"] and tool_name not in approved:
                    async with self.database.session() as session:
                        row = await session.scalar(
                            select(TaskRow).where(
                                TaskRow.id == task_id,
                                TaskRow.tenant_id == tenant_id,
                                TaskRow.lease_owner == self.worker_id,
                            )
                        )
                        if not row:
                            raise LeaseLost(f"Worker no longer owns task {task_id}")
                        self._set_status(row, TaskStatus.AWAITING_APPROVAL)
                        row.pending_action = tool_name
                        row.lease_owner = None
                        row.lease_until = None
                        await session.commit()
                    await self.audit.record(
                        tenant_id,
                        task_id,
                        self.worker_id,
                        "task.awaiting_approval",
                        {
                            "action": tool_name,
                            "reason": "reviewer_rejected_diff",
                            "risk_level": result["risk_level"],
                        },
                    )
                    await self._publish(
                        tenant_id,
                        task_id,
                        WorkerRole.REVIEWER,
                        WorkerRole.SECURITY,
                        "task.blocked",
                        {
                            "reason": "reviewer_rejected_diff",
                            "summary": result["summary"],
                            "findings": result["findings"],
                            "risk_level": result["risk_level"],
                        },
                    )
                    return
                if not result["approved"] and tool_name in approved:
                    result["human_override"] = True
            else:
                result = await self.tools.execute(tool_name, arguments)
            previous.append(
                {
                    "tool": tool_name,
                    "purpose": step["purpose"],
                    "result": result,
                }
            )
            async with self.database.session() as session:
                row = await session.scalar(
                    select(TaskRow).where(
                        TaskRow.id == task_id,
                        TaskRow.tenant_id == tenant_id,
                        TaskRow.lease_owner == self.worker_id,
                    )
                )
                if not row:
                    raise LeaseLost(f"Worker no longer owns task {task_id}")
                row.tool_results = previous
                row.cursor = cursor + 1
                row.lease_until = utcnow() + timedelta(seconds=self.lease_seconds)
                await session.commit()
            await self.audit.record(
                tenant_id,
                task_id,
                self.worker_id,
                "tool.completed",
                {"tool": tool_name, "cursor": cursor},
            )
            await self._publish(
                tenant_id,
                task_id,
                self.orchestrator.role_for_tool(tool_name),
                WorkerRole.SUPERVISOR,
                "task.progress",
                {"tool": tool_name, "cursor": cursor, "result": result},
            )

        final_answer = await self.orchestrator.synthesize(snapshot, previous)
        await self._publish(
            tenant_id,
            task_id,
            WorkerRole.SYNTHESIZER,
            WorkerRole.SUPERVISOR,
            "task.progress",
            {"synthesis_ready": True, "summary": final_answer["summary"]},
        )
        await self._verify_and_finish(tenant_id, task_id, final_answer)

    async def _verify_and_finish(
        self,
        tenant_id: str,
        task_id: str,
        final_answer: dict[str, Any],
    ) -> None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.id == task_id,
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.lease_owner == self.worker_id,
                )
            )
            if not row:
                raise LeaseLost(f"Worker no longer owns task {task_id}")
            results = row.tool_results or []
            has_branch = any(
                item["tool"] == "github.create_branch" for item in results
            )
            writes = [
                item for item in results if item["tool"] == "github.write_file"
            ]
            has_write = bool(writes)
            has_pr = any(
                item["tool"] == "github.create_pull_request" for item in results
            )
            ci_results = [
                item["result"] for item in results
                if item["tool"] == "github.get_ci_status"
            ]
            review_results = [
                item["result"] for item in results
                if item["tool"] == "agent.review_diff"
            ]
            has_comparison = any(
                item["tool"] == "github.compare_branches" for item in results
            )
            review_ok = bool(
                review_results
                and (
                    review_results[-1].get("approved")
                    or review_results[-1].get("human_override")
                )
            )
            latest_write_sha = (
                writes[-1]["result"].get("commit_sha") if writes else None
            )
            checks_ok = bool(
                ci_results
                and latest_write_sha
                and ci_results[-1].get("commit_sha") == latest_write_sha
                and ci_results[-1].get("all_passed")
            )
            if not has_branch:
                target = TaskStatus.BLOCKED
                error = "No feature branch creation evidence"
            elif has_write and not has_comparison:
                target = TaskStatus.BLOCKED
                error = "Code changes require branch diff evidence"
            elif has_write and not review_ok:
                target = TaskStatus.BLOCKED
                error = "Code changes require reviewer approval"
            elif has_write and row.require_pull_request and not has_pr:
                target = TaskStatus.BLOCKED
                error = "Code changes require a pull request"
            elif has_write and row.require_ci and not checks_ok:
                target = TaskStatus.BLOCKED
                error = "CI checks for the latest write have not all passed"
            else:
                target = TaskStatus.SUCCEEDED
                error = None
            self._set_status(row, target)
            row.error = error
            row.lease_owner = None
            row.lease_until = None
            if target == TaskStatus.SUCCEEDED:
                row.result = {
                    "tool_results": results,
                    "final_answer": final_answer,
                    "acceptance_criteria": row.acceptance_criteria,
                    "acceptance_criteria_evaluated": False,
                    "verification_scope": [
                        "feature_branch_created",
                        *(
                            ["branch_diff_reviewed"]
                            if has_write and review_ok
                            else []
                        ),
                        *(
                            ["draft_pull_request_created"]
                            if has_write and row.require_pull_request
                            else []
                        ),
                        *(
                            ["latest_write_commit_ci_passed"]
                            if has_write and row.require_ci
                            else []
                        ),
                    ],
                    "verified": True,
                }
            await session.commit()
            state = row.status
        await self.audit.record(
            tenant_id,
            task_id,
            self.worker_id,
            "task.finished",
            {"status": state, "error": error},
        )
        await self._publish(
            tenant_id,
            task_id,
            WorkerRole.SUPERVISOR,
            WorkerRole.SUPERVISOR,
            "task.result" if target == TaskStatus.SUCCEEDED else "task.blocked",
            {
                "status": state,
                "error": error,
                "final_answer": final_answer if target == TaskStatus.SUCCEEDED else None,
            },
        )

    async def _fail(self, task_id: str, error: str) -> None:
        async with self.database.session() as session:
            row = await session.scalar(
                select(TaskRow).where(
                    TaskRow.id == task_id,
                    TaskRow.status == TaskStatus.RUNNING.value,
                    TaskRow.lease_owner == self.worker_id,
                )
            )
            if not row:
                return
            row.error = error
            row.lease_owner = None
            row.lease_until = None
            if row.attempts < row.max_attempts and not row.cancel_requested:
                self._set_status(row, TaskStatus.QUEUED)
                row.available_at = utcnow() + timedelta(
                    seconds=min(300, 2 ** row.attempts * 5)
                )
            else:
                self._set_status(row, TaskStatus.FAILED)
            tenant_id, state = row.tenant_id, row.status
            await session.commit()
        await self.audit.record(
            tenant_id,
            task_id,
            self.worker_id,
            "task.retry_or_failed",
            {"status": state},
        )

    @staticmethod
    def _serialize(row: TaskRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "objective": row.objective,
            "repository": row.repository,
            "base_branch": row.base_branch,
            "work_branch": row.work_branch,
            "context": row.context,
            "acceptance_criteria": row.acceptance_criteria,
            "allowed_tools": row.allowed_tools,
            "require_ci": row.require_ci,
            "require_pull_request": row.require_pull_request,
            "max_attempts": row.max_attempts,
            "attempts": row.attempts,
            "status": row.status,
            "plan": row.plan,
            "cursor": row.cursor,
            "tool_results": row.tool_results,
            "approved_actions": row.approved_actions,
            "pending_action": row.pending_action,
            "cancel_requested": row.cancel_requested,
            "result": row.result,
            "error": row.error,
        }
