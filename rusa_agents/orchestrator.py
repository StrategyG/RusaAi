"""Hierarchical orchestrator with verified CI evidence gates."""

from __future__ import annotations

from typing import Any

from .audit import AuditLog
from .contracts import CodingTask, TaskRecord, TaskState, WorkerRole
from .memory import SQLiteMemory
from .policy import AutonomyPolicy
from .workers import AgentWorker


class AICodeOrchestrator:
    PIPELINE = (
        WorkerRole.ANALYST,
        WorkerRole.ARCHITECT,
        WorkerRole.DEVELOPER,
        WorkerRole.DEBUGGER,
        WorkerRole.TESTER,
        WorkerRole.SECURITY,
    )

    def __init__(
        self,
        workers: list[AgentWorker],
        policy: AutonomyPolicy | None = None,
        memory: SQLiteMemory | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.workers = {worker.role: worker for worker in workers}
        self.policy = policy or AutonomyPolicy()
        self.memory = memory or SQLiteMemory()
        self.audit = audit or AuditLog()
        missing = set(self.PIPELINE) - set(self.workers)
        if missing:
            raise ValueError(
                "missing required workers: " + ", ".join(role.value for role in sorted(missing, key=lambda x: x.value))
            )

    def _persist(self, record: TaskRecord) -> None:
        payload: dict[str, Any] = {
            "objective": record.task.objective,
            "acceptance_criteria": record.task.acceptance_criteria,
            "results": [
                {
                    "role": result.role.value,
                    "summary": result.summary,
                    "success": result.success,
                    "artifacts": result.artifacts,
                    "evidence": [item.__dict__ for item in result.evidence],
                    "blockers": result.blockers,
                }
                for result in record.results
            ],
            "errors": record.errors,
        }
        self.memory.save(
            record.task.task_id,
            record.task.repository.full_name,
            record.state.value,
            payload,
            tenant_id=record.task.tenant_id,
        )

    def _record(self, event: str, record: TaskRecord, details: dict[str, Any]) -> None:
        self.audit.record(
            event,
            record.task.task_id,
            "aicode-master",
            details,
            tenant_id=record.task.tenant_id,
        )

    @staticmethod
    def _has_successful_check(results, role: WorkerRole, terms: tuple[str, ...]) -> bool:
        worker_result = next((item for item in results if item.role == role), None)
        if worker_result is None:
            return False
        return any(
            evidence.kind == "ci_check"
            and evidence.status == "success"
            and any(term in evidence.name.lower() for term in terms)
            for evidence in worker_result.evidence
        )

    async def run(self, task: CodingTask) -> TaskRecord:
        record = TaskRecord(task=task)
        self._record("task.received", record, {"repository": task.repository.full_name})
        try:
            record.transition(TaskState.OBSERVING)
            self._persist(record)
            record.transition(TaskState.PLANNING)
            self._persist(record)
            if not self.policy.evaluate("repo.read").allowed:
                record.errors.append("repository read denied by autonomy policy")
                record.transition(TaskState.BLOCKED)
                return record

            record.transition(TaskState.EXECUTING)
            self._persist(record)
            for role in self.PIPELINE:
                result = await self.workers[role].run(task, list(record.results))
                record.results.append(result)
                self._record(
                    "worker.completed",
                    record,
                    {
                        "role": role.value,
                        "success": result.success,
                        "evidence_count": len(result.evidence),
                        "blockers": result.blockers,
                    },
                )
                self._persist(record)
                if not result.success or result.blockers:
                    record.errors.extend(result.blockers or [f"{role.value} worker failed"])
                    record.transition(TaskState.BLOCKED)
                    break

            if record.state == TaskState.EXECUTING:
                record.transition(TaskState.VERIFYING)
                self._persist(record)
                quality_ok = self._has_successful_check(
                    record.results,
                    WorkerRole.TESTER,
                    ("quality", "test"),
                )
                security_ok = self._has_successful_check(
                    record.results,
                    WorkerRole.SECURITY,
                    ("security", "audit"),
                )
                if not quality_ok:
                    record.errors.append("missing successful CI quality/test evidence")
                if not security_ok:
                    record.errors.append("missing successful CI security/audit evidence")
                if quality_ok and security_ok:
                    record.transition(TaskState.SUCCEEDED)
                else:
                    record.transition(TaskState.BLOCKED)
        except Exception as exc:
            record.errors.append(f"{type(exc).__name__}: {str(exc)[:1000]}")
            if record.state not in {
                TaskState.FAILED,
                TaskState.BLOCKED,
                TaskState.CANCELLED,
                TaskState.SUCCEEDED,
            }:
                record.transition(TaskState.FAILED)
            self._record("task.failed", record, {"error_type": type(exc).__name__})
        finally:
            self._persist(record)
            self._record(
                "task.finished",
                record,
                {"state": record.state.value, "errors": record.errors},
            )
        return record
