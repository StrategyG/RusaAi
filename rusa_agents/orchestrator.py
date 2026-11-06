"""Hierarchical orchestrator with strict state and evidence gates."""
from __future__ import annotations

import asyncio

from .audit import AuditLog
from .contracts import CodingTask, TaskRecord, TaskState, WorkerResult, WorkerRole
from .memory import SQLiteMemory
from .metrics import WORKER_STEPS
from .policy import AutonomyPolicy
from .workers import Worker


class AICodeOrchestrator:
    PIPELINE = (
        WorkerRole.ANALYST,
        WorkerRole.ARCHITECT,
        WorkerRole.DEVELOPER,
        WorkerRole.DEBUGGER,
        WorkerRole.TESTER,
        WorkerRole.SECURITY,
    )
    REQUIRED_TEST_EVIDENCE = ("check:pytest",)
    REQUIRED_SECURITY_EVIDENCE = ("check:bandit", "check:ruff", "check:compile")

    def __init__(
        self,
        workers: list[Worker],
        policy: AutonomyPolicy | None = None,
        memory: SQLiteMemory | None = None,
        audit: AuditLog | None = None,
    ):
        self.workers = {worker.role: worker for worker in workers}
        self.policy = policy or AutonomyPolicy()
        self.memory = memory or SQLiteMemory()
        self.audit = audit or AuditLog()
        missing = set(self.PIPELINE) - set(self.workers)
        if missing:
            raise ValueError("missing required workers: " + ", ".join(role.value for role in missing))

    @staticmethod
    def _tenant_id(task: CodingTask) -> str:
        tenant_id = str(task.metadata.get("tenant_id", "default"))
        if not tenant_id:
            raise ValueError("task tenant_id cannot be empty")
        return tenant_id

    async def _persist(self, record: TaskRecord) -> None:
        payload = {
            "objective": record.task.objective,
            "acceptance_criteria": record.task.acceptance_criteria,
            "results": [
                {
                    "role": result.role.value,
                    "summary": result.summary,
                    "success": result.success,
                    "artifacts": result.artifacts,
                    "evidence": result.evidence,
                    "blockers": result.blockers,
                }
                for result in record.results
            ],
            "errors": record.errors,
        }
        await asyncio.to_thread(
            self.memory.save,
            record.task.task_id,
            record.task.repository.full_name,
            record.state.value,
            payload,
            tenant_id=self._tenant_id(record.task),
        )

    async def _audit(
        self, event: str, task: CodingTask, actor: str, details: dict
    ) -> None:
        await asyncio.to_thread(
            self.audit.record,
            event,
            task.task_id,
            actor,
            details,
            tenant_id=self._tenant_id(task),
        )

    @staticmethod
    def _check_passed(result, key: str) -> bool:
        evidence = result.evidence.get(key)
        return isinstance(evidence, dict) and evidence.get("exit_code") == 0

    def _verify_evidence(self, record: TaskRecord) -> bool | None:
        tester = next((item for item in record.results if item.role == WorkerRole.TESTER), None)
        security = next((item for item in record.results if item.role == WorkerRole.SECURITY), None)
        if tester is None or security is None:
            record.errors.append("Tester or security worker did not produce a result.")
            return None
        required = [
            (tester, key) for key in self.REQUIRED_TEST_EVIDENCE
        ] + [
            (security, key) for key in self.REQUIRED_SECURITY_EVIDENCE
        ]
        missing = [key for worker_result, key in required if key not in worker_result.evidence]
        if missing:
            record.errors.append("Missing mandatory verification evidence: " + ", ".join(missing))
            return None
        failed = [
            key for worker_result, key in required
            if not self._check_passed(worker_result, key)
        ]
        if failed:
            record.errors.append("Validation checks failed: " + ", ".join(failed))
            return False
        return True

    async def run(self, task: CodingTask) -> TaskRecord:
        record = TaskRecord(task=task)
        previous_attempt_results: list[WorkerResult] = []
        try:
            record.transition(TaskState.OBSERVING)
            await self._audit(
                "task.received", task, "aicode-master",
                {"repository": task.repository.full_name},
            )
            previous = await asyncio.to_thread(
                self.memory.get,
                task.task_id,
                tenant_id=self._tenant_id(task),
            )
            if previous:
                previous_payload = previous.get("payload", {})
                for item in previous_payload.get("results", []):
                    try:
                        previous_attempt_results.append(
                            WorkerResult(
                                role=WorkerRole(item["role"]),
                                summary=str(item.get("summary", ""))[:4000],
                                success=bool(item.get("success", False)),
                                artifacts=list(item.get("artifacts", []))[:100],
                                evidence=dict(item.get("evidence", {})),
                                blockers=list(item.get("blockers", []))[:30],
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                prior_errors = previous_payload.get("errors", [])
                if prior_errors:
                    previous_attempt_results.append(
                        WorkerResult(
                            role=WorkerRole.ANALYST,
                            summary="Previous attempt ended in state "
                            + str(previous.get("state", "unknown"))
                            + "; errors: "
                            + "; ".join(str(error) for error in prior_errors)[:3000],
                            success=False,
                            blockers=[str(error) for error in prior_errors[:10]],
                        )
                    )
            await self._persist(record)
            record.transition(TaskState.PLANNING)
            await self._persist(record)
            if not self.policy.evaluate("repo_read").allowed:
                record.errors.append("Repository read denied by policy")
                record.transition(TaskState.BLOCKED)
                return record
            record.transition(TaskState.EXECUTING)
            await self._persist(record)
            for role in self.PIPELINE:
                action = "workspace_write" if role == WorkerRole.DEVELOPER else "repo_read"
                decision = self.policy.evaluate(action)
                if not decision.allowed:
                    record.errors.append(decision.reason)
                    record.transition(TaskState.BLOCKED)
                    break
                result = await self.workers[role].run(task, previous_attempt_results + list(record.results))
                WORKER_STEPS.labels(
                    role.value, "success" if result.success else "failure"
                ).inc()
                record.results.append(result)
                await self._audit(
                    "worker.completed", task, role.value,
                    {"success": result.success, "summary": result.summary[:500]},
                )
                await self._persist(record)
                if not result.success or result.blockers:
                    record.errors.extend(result.blockers or [role.value + " worker failed"])
                    record.transition(TaskState.BLOCKED)
                    break
            if record.state == TaskState.EXECUTING:
                record.transition(TaskState.VERIFYING)
                await self._persist(record)
                evidence_result = self._verify_evidence(record)
                if evidence_result is True:
                    record.transition(TaskState.SUCCEEDED)
                elif evidence_result is False:
                    record.transition(TaskState.BLOCKED)
                else:
                    record.transition(TaskState.AWAITING_APPROVAL)
        except Exception as exc:
            record.errors.append(type(exc).__name__ + ": " + str(exc))
            if record.state not in {TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED}:
                record.transition(TaskState.FAILED)
            await self._audit("task.failed", task, "aicode-master", {"error": str(exc)})
        finally:
            await self._persist(record)
            await self._audit(
                "task.finished", task, "aicode-master",
                {"state": record.state.value, "errors": record.errors},
            )
        return record
