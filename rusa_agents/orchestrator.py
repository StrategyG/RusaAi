"""Hierarchical worker orchestration with a fail-closed evidence gate."""

import asyncio

from .audit import AuditLog
from .contracts import CodingTask, EvidenceKind, TaskRecord, TaskState, WorkerRole
from .evidence import EvidenceVerifier, RejectUnverifiedEvidence
from .memory import SQLiteMemory
from .policy import AutonomyPolicy, RiskLevel
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
    ACTIONS = {
        WorkerRole.ANALYST: "repo_read",
        WorkerRole.ARCHITECT: "repo_read",
        WorkerRole.DEVELOPER: "workspace_write",
        WorkerRole.DEBUGGER: "workspace_write",
        WorkerRole.TESTER: "test_run",
        WorkerRole.SECURITY: "security_scan",
    }

    def __init__(
        self,
        workers: list[Worker],
        memory: SQLiteMemory,
        audit: AuditLog,
        policy: AutonomyPolicy | None = None,
        evidence_verifier: EvidenceVerifier | None = None,
        worker_timeout_seconds: float = 300,
    ) -> None:
        if worker_timeout_seconds <= 0:
            raise ValueError("worker_timeout_seconds must be positive")
        roles = [worker.role for worker in workers]
        if len(roles) != len(set(roles)):
            raise ValueError("worker roles must be unique")
        self.workers = {worker.role: worker for worker in workers}
        missing = set(self.PIPELINE) - set(self.workers)
        if missing:
            raise ValueError(
                "missing workers: " + ", ".join(sorted(role.value for role in missing))
            )
        self.memory = memory
        self.audit = audit
        self.policy = policy or AutonomyPolicy()
        self.verifier = evidence_verifier or RejectUnverifiedEvidence()
        self.timeout = worker_timeout_seconds

    def _persist(self, record: TaskRecord) -> None:
        payload = {
            "objective": record.task.objective,
            "acceptance_criteria": record.task.acceptance_criteria,
            "results": [
                {
                    "role": result.role.value,
                    "summary": result.summary[:4000],
                    "success": result.success,
                    "blockers": result.blockers[:100],
                    "evidence": [
                        {
                            "kind": item.kind.value,
                            "reference": item.reference,
                            "commit_sha": item.commit_sha,
                        }
                        for item in result.evidence
                    ],
                }
                for result in record.results
            ],
            "errors": record.errors[:100],
        }
        self.memory.save(
            record.task.tenant_id,
            record.task.task_id,
            record.task.repository.full_name,
            record.state.value,
            payload,
        )

    async def _completion_verified(self, record: TaskRecord) -> bool:
        tester = next(
            (item for item in record.results if item.role == WorkerRole.TESTER),
            None,
        )
        security = next(
            (item for item in record.results if item.role == WorkerRole.SECURITY),
            None,
        )
        if tester is None or security is None:
            return False

        tests = [
            item for item in tester.evidence if item.kind == EvidenceKind.TEST_RUN
        ]
        scans = [
            item for item in security.evidence if item.kind == EvidenceKind.SECURITY_SCAN
        ]
        if not tests or not scans:
            return False

        test_shas = {item.commit_sha.lower() for item in tests}
        scan_shas = {item.commit_sha.lower() for item in scans}
        if len(test_shas) != 1 or test_shas != scan_shas:
            return False

        checks = [
            self.verifier.verify(item, record.task)
            for item in tests + scans
        ]
        return all(await asyncio.gather(*checks))

    async def run(self, task: CodingTask) -> TaskRecord:
        record = TaskRecord(task)
        self.audit.record(
            "task.received",
            task.task_id,
            "aicode-master",
            {
                "tenant_id": task.tenant_id,
                "repository": task.repository.full_name,
            },
        )
        try:
            record.transition(TaskState.OBSERVING)
            self._persist(record)
            record.transition(TaskState.PLANNING)
            self._persist(record)

            decision = self.policy.evaluate("repo_read")
            if decision.requires_approval:
                record.errors.append(decision.reason)
                record.transition(TaskState.AWAITING_APPROVAL)
                return record
            if not decision.allowed:
                record.errors.append(decision.reason)
                record.transition(TaskState.BLOCKED)
                return record

            record.transition(TaskState.EXECUTING)
            self._persist(record)

            for role in self.PIPELINE:
                action = self.ACTIONS[role]
                risk = (
                    RiskLevel.MEDIUM
                    if action == "workspace_write"
                    else RiskLevel.LOW
                )
                decision = self.policy.evaluate(action, risk)
                if decision.requires_approval:
                    record.errors.append(decision.reason)
                    record.transition(TaskState.AWAITING_APPROVAL)
                    break
                if not decision.allowed:
                    record.errors.append(decision.reason)
                    record.transition(TaskState.BLOCKED)
                    break

                try:
                    result = await asyncio.wait_for(
                        self.workers[role].run(task, list(record.results)),
                        timeout=self.timeout,
                    )
                except asyncio.TimeoutError:
                    record.errors.append(f"{role.value} worker timed out")
                    record.transition(TaskState.BLOCKED)
                    break

                if result.role != role:
                    record.errors.append(
                        f"{role.value} returned mismatched worker role"
                    )
                    record.transition(TaskState.BLOCKED)
                    break

                record.results.append(result)
                self.audit.record(
                    "worker.completed",
                    task.task_id,
                    role.value,
                    {
                        "success": result.success,
                        "evidence_count": len(result.evidence),
                        "blocker_count": len(result.blockers),
                    },
                )
                self._persist(record)

                if not result.success or result.blockers:
                    record.errors.extend(
                        result.blockers or [f"{role.value} worker failed"]
                    )
                    record.transition(TaskState.BLOCKED)
                    break

            if record.state == TaskState.EXECUTING:
                record.transition(TaskState.VERIFYING)
                self._persist(record)
                if await self._completion_verified(record):
                    record.transition(TaskState.SUCCEEDED)
                else:
                    record.errors.append(
                        "Missing independently verified test and security evidence"
                    )
                    record.transition(TaskState.BLOCKED)

        except Exception as exc:
            record.errors.append(f"runtime failure: {type(exc).__name__}")
            if record.state not in {
                TaskState.FAILED,
                TaskState.BLOCKED,
                TaskState.CANCELLED,
                TaskState.SUCCEEDED,
            }:
                record.transition(TaskState.FAILED)
            self.audit.record(
                "task.failed",
                task.task_id,
                "aicode-master",
                {"exception_type": type(exc).__name__},
            )
        finally:
            self._persist(record)
            self.audit.record(
                "task.finished",
                task.task_id,
                "aicode-master",
                {
                    "state": record.state.value,
                    "error_count": len(record.errors),
                },
            )
        return record
