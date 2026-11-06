"""Hierarchical master orchestrator with checkpointed CI verification and bounded repair."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from .contracts import (
    CodingTask, RiskLevel, TaskRecord, TaskState, ToolCall, ToolResult, WorkerResult, WorkerRole,
)
from .policy import AutonomyPolicy
from .workers import AgentWorker


class ToolGateway(Protocol):
    async def execute(self, task: CodingTask, call: Any) -> ToolResult: ...


class AuditSink(Protocol):
    def record(
        self, event: str, *, tenant_id: str, task_id: str, actor: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    def verify(self) -> bool: ...


class CheckpointPersistenceError(RuntimeError):
    """Raised when a durable task checkpoint cannot be saved."""


CheckpointCallback = Callable[[TaskRecord], Awaitable[None]]


class AICodeOrchestrator:
    ROLE_TOOLS = AgentWorker.ROLE_TOOLS
    ACTIONS = {
        "create_branch": "branch_create", "read_file": "repo_read",
        "list_files": "repo_read", "write_file": "repo_write",
        "create_pull_request": "pull_request_create", "get_ci_status": "ci_read",
    }

    def __init__(
        self, *, workers: list[AgentWorker], tools: ToolGateway, audit: AuditSink,
        policy: AutonomyPolicy | None = None, worker_timeout_seconds: float = 120.0,
        max_repair_cycles: int = 2,
        required_ci_checks: tuple[str, ...] = (
            "Lint, test, and audit dependencies", "Build and smoke-test containers",
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
        self.tools = tools
        self.audit = audit
        self.policy = policy or AutonomyPolicy()
        self.worker_timeout_seconds = worker_timeout_seconds
        self.max_repair_cycles = max_repair_cycles
        self.required_ci_checks = set(required_ci_checks)

    def _event(self, event: str, record: TaskRecord, actor: str, details: dict[str, Any]) -> None:
        self.audit.record(
            event, tenant_id=record.task.tenant_id, task_id=record.task.task_id,
            actor=actor, details=details,
        )

    @staticmethod
    async def _persist(
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
            raise _ApprovalRequired(f"{role.value} assessed task risk as {result.risk.value}")
        tool_results = []
        for call in result.tool_calls:
            if call.name not in self.ROLE_TOOLS[role]:
                raise _TaskBlocked(f"{role.value} is not authorized to use {call.name}")
            action = self.ACTIONS[call.name]
            path = str(call.arguments.get("path", "")) if call.name in {
                "read_file", "list_files", "write_file",
            } else None
            decision = self.policy.evaluate(
                action, risk=result.risk, repository=record.task.repository, path=path,
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
                "tool.completed", record, role.value,
                {"tool": call.name, "success": tool_result.success, "evidence": dict(tool_result.evidence)},
            )
            if not tool_result.success:
                raise _TaskBlocked(f"tool {call.name} returned an unsuccessful result")
        completed = replace(result, tool_results=tuple(tool_results))
        record.worker_results.append(completed)
        self._event(
            "worker.completed", record, role.value,
            {"tool_count": len(tool_results), "summary": result.summary[:400]},
        )
        await self._persist(record, checkpoint_callback)
        return completed

    def _ci_outcome(self, result: ToolResult) -> str:
        sha = result.data.get("head_sha")
        if (
            result.name != "get_ci_status"
            or result.success is not True
            or result.evidence.get("source") != "github_check_runs_api"
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or result.evidence.get("head_sha") != sha
            or result.data.get("checks_complete") is not True
        ):
            return "pending"
        checks = result.data.get("checks", [])
        if not isinstance(checks, list) or not checks:
            return "pending"
        if any(item.get("status") != "completed" for item in checks):
            return "pending"
        required = {
            item.get("name"): item for item in checks
            if item.get("name") in self.required_ci_checks
        }
        if not self.required_ci_checks.issubset(required):
            return "failed"
        if any(required[name].get("conclusion") != "success" for name in self.required_ci_checks):
            return "failed"
        return "passed"

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
        self._event("task.started", record, "rusa-master", {"repository": task.repository.full_name})
        try:
            if record.state == TaskState.WAITING_EXTERNAL:
                record.transition(TaskState.VERIFYING)
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
                    raise _TaskBlocked(f"invalid orchestration checkpoint stage: {stage}")

                if record.state != target:
                    record.transition(target)
                await self._persist(record, checkpoint_callback)
                result = await self._run_worker(record, role, checkpoint_callback)

                if stage in {0, 1, 2}:
                    record.stage_index += 1
                    await self._persist(record, checkpoint_callback)
                    continue
                if stage == 3:
                    ci = next(
                        (item for item in reversed(result.tool_results) if item.name == "get_ci_status"),
                        None,
                    )
                    if ci is None:
                        raise _TaskBlocked("tester must provide GitHub CI evidence")
                    outcome = self._ci_outcome(ci)
                    if outcome == "pending":
                        record.stage_index = 3
                        record.transition(TaskState.WAITING_EXTERNAL)
                        self._event(
                            "task.waiting_external", record, "rusa-master", {"reason": "CI pending"},
                        )
                        await self._persist(record, checkpoint_callback)
                        return record
                    if outcome == "failed":
                        if record.repair_cycles >= self.max_repair_cycles:
                            raise _TaskBlocked("CI failed after the maximum repair cycles")
                        record.stage_index = 5
                        record.repair_cycles += 1
                        await self._persist(record, checkpoint_callback)
                        continue
                    if not self._criteria_passed(task, result):
                        raise _TaskBlocked("tester did not verify every acceptance criterion")
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
                    if result.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL} or result.blockers:
                        raise _TaskBlocked("security evaluation did not pass")
                    if not self._criteria_passed(task, result):
                        raise _TaskBlocked("security worker did not verify every acceptance criterion")

                    tester_result = next(
                        (item for item in reversed(record.worker_results)
                         if item.role == WorkerRole.TESTER),
                        None,
                    )
                    tester_ci = next(
                        (item for item in reversed(tester_result.tool_results)
                         if item.name == "get_ci_status"),
                        None,
                    ) if tester_result else None
                    if tester_ci is None or self._ci_outcome(tester_ci) != "passed":
                        raise _TaskBlocked("tester CI evidence is missing or unverifiable")

                    decision = self.policy.evaluate(
                        "ci_read", risk=RiskLevel.LOW, repository=task.repository,
                    )
                    if not decision.allowed:
                        raise _TaskBlocked("final CI verification is denied by policy")
                    final_ci = await asyncio.wait_for(
                        self.tools.execute(task, ToolCall("get_ci_status")),
                        timeout=self.worker_timeout_seconds,
                    )
                    final_sha = final_ci.data.get("head_sha")
                    tester_sha = tester_ci.data.get("head_sha")
                    final_outcome = self._ci_outcome(final_ci)
                    if final_outcome != "passed" or final_sha != tester_sha:
                        record.stage_index = 3
                        record.transition(TaskState.WAITING_EXTERNAL)
                        self._event(
                            "task.waiting_external", record, "rusa-master",
                            {
                                "reason": "final CI evidence requires re-verification",
                                "tested_sha": tester_sha,
                                "current_sha": final_sha,
                                "ci_outcome": final_outcome,
                            },
                        )
                        await self._persist(record, checkpoint_callback)
                        return record
                    record.stage_index = 7
                    await self._persist(record, checkpoint_callback)
                    continue

            if record.stage_index == 7:
                self._event(
                    "task.completion_candidate", record, "rusa-master",
                    {"worker_count": len(record.worker_results), "repair_cycles": record.repair_cycles},
                )
                try:
                    audit_valid = self.audit.verify()
                except Exception as exc:
                    raise _TaskBlocked("audit integrity verification failed") from exc
                if not audit_valid:
                    raise _TaskBlocked("audit integrity verification failed")
                record.transition(TaskState.SUCCEEDED)
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
                "task.checkpoint", record, "rusa-master",
                {"state": record.state.value, "stage_index": record.stage_index},
            )
        await self._persist(record, checkpoint_callback)
        return record


class _TaskBlocked(RuntimeError):
    pass


class _ApprovalRequired(RuntimeError):
    pass
