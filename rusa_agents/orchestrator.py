"""Hierarchical master orchestrator with fenced checkpoints and commit-bound evidence."""

import asyncio
import re
from dataclasses import replace
from typing import Any, Awaitable, Callable, Protocol

from .contracts import (
    CodingTask, RiskLevel, TaskRecord, TaskState, ToolResult, WorkerResult, WorkerRole,
)
from .policy import AutonomyPolicy
from .workers import AgentWorker


class ToolGateway(Protocol):
    async def execute(self, task: CodingTask, call: Any) -> ToolResult: ...


class AuditSink(Protocol):
    def record(
        self,
        event: str,
        *,
        tenant_id: str,
        task_id: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    def verify(self) -> bool: ...


CheckpointCallback = Callable[[TaskRecord], Awaitable[None]]


class CheckpointPersistenceError(RuntimeError):
    """Raised when a durable worker checkpoint cannot be persisted."""


class AICodeOrchestrator:
    ROLE_TOOLS = AgentWorker.ROLE_TOOLS
    ACTIONS = {
        "create_branch": "branch_create",
        "read_file": "repo_read",
        "list_files": "repo_read",
        "write_file": "repo_write",
        "create_pull_request": "pull_request_create",
        "get_ci_status": "ci_read",
        "get_branch_diff": "repo_read",
    }
    SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

    def __init__(
        self,
        *,
        workers: list[AgentWorker],
        tools: ToolGateway,
        audit: AuditSink,
        policy: AutonomyPolicy | None = None,
        worker_timeout_seconds: float = 120.0,
        max_repair_cycles: int = 2,
        required_ci_checks: tuple[str, ...] = (
            "Lint, test, and audit dependencies",
            "Build and smoke-test containers",
        ),
    ) -> None:
        self.workers = {worker.role: worker for worker in workers}
        missing = set(WorkerRole) - set(self.workers)
        if missing:
            raise ValueError(
                "missing required workers: " + ", ".join(sorted(role.value for role in missing))
            )
        if worker_timeout_seconds <= 0 or not 0 <= max_repair_cycles <= 5:
            raise ValueError("invalid orchestration limits")
        if not required_ci_checks or len(set(required_ci_checks)) != len(required_ci_checks):
            raise ValueError("required CI checks must be non-empty and unique")
        self.tools = tools
        self.audit = audit
        self.policy = policy or AutonomyPolicy()
        self.worker_timeout_seconds = worker_timeout_seconds
        self.max_repair_cycles = max_repair_cycles
        self.required_ci_checks = set(required_ci_checks)

    def _event(
        self,
        event: str,
        record: TaskRecord,
        actor: str,
        details: dict[str, Any],
    ) -> None:
        self.audit.record(
            event,
            tenant_id=record.task.tenant_id,
            task_id=record.task.task_id,
            actor=actor,
            details=details,
        )

    async def _persist(
        self,
        record: TaskRecord,
        callback: CheckpointCallback | None,
    ) -> None:
        if callback is None:
            return
        try:
            await callback(record)
        except Exception as exc:
            raise CheckpointPersistenceError(
                "durable task checkpoint could not be persisted"
            ) from exc

    @staticmethod
    def _criteria_passed(task: CodingTask, result: WorkerResult) -> bool:
        expected = set(task.acceptance_criteria)
        return (
            set(result.criteria_results) == expected
            and all(result.criteria_results.get(item) is True for item in expected)
        )

    @staticmethod
    def _tool_result(result: WorkerResult, name: str) -> ToolResult | None:
        return next(
            (item for item in reversed(result.tool_results) if item.name == name),
            None,
        )

    def _ci_outcome(self, result: ToolResult) -> str:
        if not result.success or result.name != "get_ci_status":
            return "pending"
        head_sha = result.data.get("head_sha")
        if (
            not isinstance(head_sha, str)
            or not self.SHA_PATTERN.fullmatch(head_sha)
            or result.evidence.get("source") != "github_check_runs_api"
            or result.evidence.get("head_sha") != head_sha
            or result.data.get("checks_complete") is not True
        ):
            return "pending"
        checks = result.data.get("checks")
        total_count = result.data.get("total_count")
        if (
            not isinstance(checks, list)
            or not checks
            or type(total_count) is not int
            or total_count != len(checks)
        ):
            return "pending"
        by_name = {
            item.get("name"): item for item in checks if isinstance(item, dict)
        }
        if not self.required_ci_checks.issubset(by_name):
            return "pending"
        if any(
            not isinstance(item, dict) or item.get("status") != "completed"
            for item in checks
        ):
            return "pending"
        if any(item.get("conclusion") != "success" for item in checks):
            return "failed"
        return "passed"

    async def _run_worker(
        self,
        record: TaskRecord,
        role: WorkerRole,
        checkpoint_callback: CheckpointCallback | None,
    ) -> WorkerResult:
        result = await asyncio.wait_for(
            self.workers[role].run(record.task, list(record.worker_results)),
            timeout=self.worker_timeout_seconds,
        )
        if result.blockers:
            record.worker_results.append(result)
            record.errors.extend(result.blockers)
            raise _TaskBlocked("; ".join(result.blockers))
        if result.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            record.worker_results.append(result)
            raise _ApprovalRequired(
                f"{role.value} assessed task risk as {result.risk.value}"
            )
        tool_results = []
        for call in result.tool_calls:
            if call.name not in self.ROLE_TOOLS[role]:
                raise _TaskBlocked(f"{role.value} is not authorized to use {call.name}")
            action = self.ACTIONS[call.name]
            path = (
                str(call.arguments.get("path", ""))
                if call.name in {"read_file", "list_files", "write_file"}
                else None
            )
            decision = self.policy.evaluate(
                action,
                risk=result.risk,
                repository=record.task.repository,
                path=path,
            )
            if not decision.allowed:
                if decision.requires_approval:
                    raise _ApprovalRequired(decision.reason)
                raise _TaskBlocked(decision.reason)
            tool_result = await asyncio.wait_for(
                self.tools.execute(record.task, call),
                timeout=self.worker_timeout_seconds,
            )
            tool_results.append(tool_result)
            self._event(
                "tool.completed",
                record,
                role.value,
                {
                    "tool": call.name,
                    "success": tool_result.success,
                    "evidence": dict(tool_result.evidence),
                },
            )
            if not tool_result.success:
                raise _TaskBlocked(f"tool {call.name} returned an unsuccessful result")
        completed = replace(result, tool_results=tuple(tool_results))
        record.worker_results.append(completed)
        self._event(
            "worker.completed",
            record,
            role.value,
            {"tool_count": len(tool_results), "summary": result.summary[:400]},
        )
        await self._persist(record, checkpoint_callback)
        return completed

    async def _run_security_review(
        self,
        record: TaskRecord,
        checkpoint_callback: CheckpointCallback | None,
    ) -> WorkerResult:
        review = await asyncio.wait_for(
            self.workers[WorkerRole.SECURITY].run(
                record.task,
                list(record.worker_results),
                review_only=True,
            ),
            timeout=self.worker_timeout_seconds,
        )
        if review.role != WorkerRole.SECURITY or review.tool_calls:
            raise _TaskBlocked("security review phase must be read-only and tool-free")
        record.worker_results.append(review)
        if review.blockers:
            record.errors.extend(review.blockers)
            raise _TaskBlocked("; ".join(review.blockers))
        if review.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            raise _ApprovalRequired(
                f"security review assessed task risk as {review.risk.value}"
            )
        self._event(
            "worker.completed",
            record,
            "security-review",
            {"tool_count": 0, "summary": review.summary[:400]},
        )
        await self._persist(record, checkpoint_callback)
        return review

    def _tester_sha(self, record: TaskRecord) -> str | None:
        tester = next(
            (
                item for item in reversed(record.worker_results)
                if item.role == WorkerRole.TESTER
            ),
            None,
        )
        if tester is None:
            return None
        ci = self._tool_result(tester, "get_ci_status")
        if ci is None or self._ci_outcome(ci) != "passed":
            return None
        return str(ci.data.get("head_sha"))

    async def _wait_for_ci(
        self,
        record: TaskRecord,
        reason: str,
        checkpoint_callback: CheckpointCallback | None,
    ) -> TaskRecord:
        record.stage_index = 3
        record.transition(TaskState.WAITING_EXTERNAL)
        self._event("task.waiting_external", record, "rusa-master", {"reason": reason})
        await self._persist(record, checkpoint_callback)
        return record

    async def run(
        self,
        task: CodingTask,
        record: TaskRecord | None = None,
        *,
        checkpoint_callback: CheckpointCallback | None = None,
    ) -> TaskRecord:
        record = record or TaskRecord(task=task)
        if record.task.task_id != task.task_id or record.task.tenant_id != task.tenant_id:
            raise ValueError("checkpoint does not belong to the supplied task")
        if record.state in {
            TaskState.SUCCEEDED, TaskState.BLOCKED, TaskState.FAILED,
            TaskState.CANCELLED, TaskState.AWAITING_APPROVAL,
        }:
            return record

        resume_external = record.state == TaskState.WAITING_EXTERNAL
        try:
            self._event(
                "task.started",
                record,
                "rusa-master",
                {"repository": task.repository.full_name},
            )
            await self._persist(record, checkpoint_callback)
            if record.state == TaskState.WAITING_EXTERNAL:
                record.transition(TaskState.VERIFYING)
                await self._persist(record, checkpoint_callback)

            while True:
                stage = record.stage_index
                if stage == 0:
                    role, target = WorkerRole.ANALYST, TaskState.OBSERVING
                elif stage == 1:
                    role, target = WorkerRole.ARCHITECT, TaskState.PLANNING
                elif stage in {2, 6}:
                    role, target = WorkerRole.DEVELOPER, TaskState.EXECUTING
                elif stage == 3:
                    role, target = WorkerRole.TESTER, TaskState.VERIFYING
                elif stage == 4:
                    role, target = WorkerRole.SECURITY, TaskState.VERIFYING
                elif stage == 5:
                    role, target = WorkerRole.DEBUGGER, TaskState.EXECUTING
                elif stage == 7:
                    break
                else:
                    raise _TaskBlocked(
                        f"invalid orchestration checkpoint stage: {stage}"
                    )

                if record.state != target:
                    record.transition(target)
                    await self._persist(record, checkpoint_callback)
                force_refresh = resume_external and stage == 3
                if stage == 3:
                    resume_external = False
                previous = record.worker_results[-1] if record.worker_results else None
                can_reuse_checkpointed_result = (
                    not force_refresh
                    and previous is not None
                    and previous.role == role
                    and (
                        role in {WorkerRole.ANALYST, WorkerRole.ARCHITECT}
                        or bool(previous.tool_results)
                    )
                )
                if can_reuse_checkpointed_result:
                    result = previous
                else:
                    result = await self._run_worker(
                        record,
                        role,
                        checkpoint_callback,
                    )

                if stage in {0, 1}:
                    record.stage_index += 1
                    await self._persist(record, checkpoint_callback)
                    continue

                if stage == 2:
                    pr = self._tool_result(result, "create_pull_request")
                    if (
                        pr is None
                        or type(pr.data.get("number")) is not int
                        or not isinstance(pr.data.get("url"), str)
                        or pr.data.get("draft") is not True
                        or pr.evidence.get("source") != "github_pull_requests_api"
                    ):
                        raise _TaskBlocked(
                            "developer must create or reuse a confirmed draft pull request"
                        )
                    record.stage_index = 3
                    await self._persist(record, checkpoint_callback)
                    continue

                if stage == 3:
                    ci = self._tool_result(result, "get_ci_status")
                    if ci is None:
                        raise _TaskBlocked("tester must provide GitHub CI evidence")
                    outcome = self._ci_outcome(ci)
                    if outcome == "pending":
                        return await self._wait_for_ci(
                            record,
                            "CI evidence is incomplete or still running",
                            checkpoint_callback,
                        )
                    if outcome == "failed":
                        if record.repair_cycles >= self.max_repair_cycles:
                            raise _TaskBlocked(
                                "CI failed after the maximum repair cycles"
                            )
                        record.stage_index = 5
                        record.repair_cycles += 1
                        await self._persist(record, checkpoint_callback)
                        continue
                    if not self._criteria_passed(task, result):
                        raise _TaskBlocked(
                            "tester did not verify every acceptance criterion"
                        )
                    record.stage_index = 4
                    await self._persist(record, checkpoint_callback)
                    continue

                if stage == 5:
                    record.stage_index = 6
                    await self._persist(record, checkpoint_callback)
                    continue

                if stage == 6:
                    record.stage_index = 3
                    await self._persist(record, checkpoint_callback)
                    continue

                if stage == 4:
                    tester_sha = self._tester_sha(record)
                    ci = self._tool_result(result, "get_ci_status")
                    diff = self._tool_result(result, "get_branch_diff")
                    if ci is None or diff is None:
                        raise _TaskBlocked(
                            "security worker must provide fresh CI and complete branch-diff evidence"
                        )
                    outcome = self._ci_outcome(ci)
                    if outcome == "pending":
                        return await self._wait_for_ci(
                            record,
                            "security-stage CI evidence is incomplete or still running",
                            checkpoint_callback,
                        )
                    security_sha = str(ci.data.get("head_sha", ""))
                    diff_sha = str(diff.data.get("head_sha", ""))
                    if (
                        tester_sha is None
                        or security_sha != tester_sha
                        or diff_sha != tester_sha
                        or diff.evidence.get("source") != "github_compare_api"
                        or diff.evidence.get("head_sha") != diff_sha
                    ):
                        return await self._wait_for_ci(
                            record,
                            "branch head changed between test and security verification",
                            checkpoint_callback,
                        )
                    if outcome == "failed":
                        if record.repair_cycles >= self.max_repair_cycles:
                            raise _TaskBlocked(
                                "CI failed during security verification after the repair budget"
                            )
                        record.stage_index = 5
                        record.repair_cycles += 1
                        await self._persist(record, checkpoint_callback)
                        continue
                    files = diff.data.get("files")
                    changed_files = diff.data.get("changed_files")
                    if (
                        diff.data.get("diff_complete") is not True
                        or type(changed_files) is not int
                        or changed_files < 1
                        or not isinstance(files, list)
                        or len(files) != changed_files
                        or any(
                            not isinstance(item, dict)
                            or not isinstance(item.get("path"), str)
                            or not item.get("path")
                            or not isinstance(item.get("patch"), str)
                            for item in files
                        )
                    ):
                        raise _TaskBlocked(
                            "security review requires a complete, non-empty, bounded repository diff"
                        )
                    if any(
                        "[REDACTED" in item.get("patch", "")
                        for item in files
                    ):
                        raise _TaskBlocked(
                            "repository diff contains potential secret material; remediation is required"
                        )
                    review = await self._run_security_review(
                        record,
                        checkpoint_callback,
                    )
                    if not self._criteria_passed(task, review):
                        raise _TaskBlocked(
                            "security review did not verify every acceptance criterion"
                        )
                    record.stage_index = 7
                    await self._persist(record, checkpoint_callback)
                    continue

            if record.stage_index == 7:
                self._event(
                    "task.completion_candidate",
                    record,
                    "rusa-master",
                    {
                        "worker_count": len(record.worker_results),
                        "repair_cycles": record.repair_cycles,
                    },
                )
                try:
                    audit_valid = self.audit.verify()
                except Exception as exc:
                    raise _TaskBlocked("audit integrity verification failed") from exc
                if not audit_valid:
                    raise _TaskBlocked("audit integrity verification failed")
                record.transition(TaskState.SUCCEEDED)
                self._event(
                    "task.succeeded",
                    record,
                    "rusa-master",
                    {"stage_index": record.stage_index},
                )
                await self._persist(record, checkpoint_callback)

        except CheckpointPersistenceError:
            raise
        except _ApprovalRequired as exc:
            record.errors.append(str(exc))
            if record.state != TaskState.AWAITING_APPROVAL:
                record.transition(TaskState.AWAITING_APPROVAL)
        except _TaskBlocked as exc:
            record.errors.append(str(exc))
            if record.state not in {TaskState.BLOCKED, TaskState.FAILED}:
                record.transition(TaskState.BLOCKED)
        except asyncio.TimeoutError:
            record.errors.append("worker or tool execution timed out")
            if record.state not in {TaskState.FAILED, TaskState.BLOCKED}:
                record.transition(TaskState.FAILED)
        except Exception as exc:
            record.errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")
            if record.state not in {TaskState.FAILED, TaskState.BLOCKED}:
                record.transition(TaskState.FAILED)
        finally:
            self._event(
                "task.checkpoint",
                record,
                "rusa-master",
                {"state": record.state.value, "stage_index": record.stage_index},
            )
            await self._persist(record, checkpoint_callback)
        return record


class _TaskBlocked(RuntimeError):
    pass


class _ApprovalRequired(RuntimeError):
    pass
