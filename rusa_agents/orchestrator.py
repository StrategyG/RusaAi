"""Hierarchical orchestrator with commit-bound, source-verified CI evidence gates."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

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
        memory: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self.workers = {worker.role: worker for worker in workers}
        self.policy = policy or AutonomyPolicy()
        self.memory = memory or SQLiteMemory()
        self.audit = audit or AuditLog()
        missing = set(self.PIPELINE) - set(self.workers)
        if missing:
            raise ValueError(
                "missing required workers: "
                + ", ".join(role.value for role in sorted(missing, key=lambda x: x.value))
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
    def _valid_source(source: str, repository: str, kind: str) -> bool:
        try:
            parsed = urlsplit(source)
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            return False
        path = parsed.path.rstrip("/")
        prefix = f"/{repository}/"
        if not path.startswith(prefix):
            return False
        if kind == "pull_request":
            return bool(re.fullmatch(re.escape(prefix) + r"pull/[1-9][0-9]*", path))
        return any(segment in path for segment in ("/runs/", "/actions/", "/checks/"))

    @classmethod
    def _evidence_shas(
        cls,
        results,
        role: WorkerRole,
        repository: str,
        *,
        kind: str,
        name_terms: tuple[str, ...] = (),
        status: str,
    ) -> set[str]:
        worker_result = next((item for item in results if item.role == role), None)
        if worker_result is None:
            return set()
        matches = set()
        for evidence in worker_result.evidence:
            if evidence.kind != kind or evidence.status != status:
                continue
            if name_terms and not any(term in evidence.name.lower() for term in name_terms):
                continue
            if not re.fullmatch(r"[0-9a-fA-F]{40}", evidence.sha):
                continue
            if not cls._valid_source(evidence.source, repository, kind):
                continue
            matches.add(evidence.sha.lower())
        return matches

    def _verify_completion(self, record: TaskRecord) -> tuple[bool, list[str]]:
        repository = record.task.repository.full_name
        errors: list[str] = []
        pull_request_shas = self._evidence_shas(
            record.results,
            WorkerRole.DEVELOPER,
            repository,
            kind="pull_request",
            status="draft",
        )
        quality_shas = self._evidence_shas(
            record.results,
            WorkerRole.TESTER,
            repository,
            kind="ci_check",
            name_terms=("quality", "test"),
            status="success",
        )
        security_shas = self._evidence_shas(
            record.results,
            WorkerRole.SECURITY,
            repository,
            kind="ci_check",
            name_terms=("security", "audit"),
            status="success",
        )
        if len(pull_request_shas) != 1:
            errors.append("missing or ambiguous verified draft pull-request evidence")
        if len(quality_shas) != 1:
            errors.append("missing or ambiguous successful quality/test evidence")
        if len(security_shas) != 1:
            errors.append("missing or ambiguous successful security/audit evidence")
        if (
            len(pull_request_shas) == 1
            and len(quality_shas) == 1
            and len(security_shas) == 1
            and pull_request_shas == quality_shas == security_shas
        ):
            return True, []
        if pull_request_shas and quality_shas and pull_request_shas != quality_shas:
            errors.append("quality/test evidence is not bound to the draft PR head SHA")
        if pull_request_shas and security_shas and pull_request_shas != security_shas:
            errors.append("security/audit evidence is not bound to the draft PR head SHA")
        return False, errors

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
                verified, evidence_errors = self._verify_completion(record)
                if verified:
                    record.transition(TaskState.SUCCEEDED)
                else:
                    record.errors.extend(evidence_errors)
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
