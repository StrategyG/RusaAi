"""Common domain-orchestrator interface and backward-compatible AICode adapter."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .contracts import CodingTask, TaskRecord


class DomainOrchestratorError(RuntimeError):
    """A domain operation is unavailable or outside its explicit scope."""


@dataclass(frozen=True)
class DomainPlan:
    tenant_id: str
    domain: str
    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    execution_mode: str
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainAccessGrant:
    tenant_id: str
    source_domain: str
    target_domain: str
    actor: str
    grant_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not all((
            self.tenant_id, self.source_domain, self.target_domain,
            self.actor, self.grant_id,
        )):
            raise ValueError("cross-domain grant identity is required")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("cross-domain grant expiry must be timezone-aware")


def authorize_domain_handoff(
    *,
    tenant_id: str,
    source_domain: str,
    target_domain: str,
    grant: DomainAccessGrant | None,
    now: datetime | None = None,
) -> None:
    if source_domain == target_domain:
        return
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("cross-domain authorization time must be timezone-aware")
    if grant is None:
        raise DomainOrchestratorError("cross-domain handoff requires an explicit platform grant")
    if (
        grant.tenant_id != tenant_id
        or grant.source_domain != source_domain
        or grant.target_domain != target_domain
        or instant.astimezone(timezone.utc) >= grant.expires_at.astimezone(timezone.utc)
    ):
        raise DomainOrchestratorError("cross-domain grant is invalid, expired, or out of scope")


class DomainOrchestrator(Protocol):
    domain: str

    async def plan(self, task: CodingTask) -> DomainPlan: ...
    async def dispatch(self, task: CodingTask) -> TaskRecord: ...
    async def progress(self, tenant_id: str, task_id: str) -> Mapping[str, Any]: ...
    async def cancel(self, tenant_id: str, task_id: str) -> bool: ...
    async def validate(self, record: TaskRecord) -> Mapping[str, Any]: ...
    async def final_report(self, record: TaskRecord) -> Mapping[str, Any]: ...


ProgressReader = Callable[[str, str], Awaitable[Mapping[str, Any]]]
CancelHandler = Callable[[str, str], Awaitable[bool]]
Validator = Callable[[TaskRecord], Awaitable[Mapping[str, Any]]]


class AICodeDomainOrchestratorAdapter:
    """Wrap AICodeOrchestrator without replacing its existing task contract or run method."""

    domain = "software_engineering"

    def __init__(
        self,
        orchestrator: Any,
        *,
        progress_reader: ProgressReader | None = None,
        cancel_handler: CancelHandler | None = None,
        validator: Validator | None = None,
    ):
        if orchestrator is None or not callable(getattr(orchestrator, "run", None)):
            raise ValueError("AICodeOrchestrator-compatible object with async run(task) is required")
        self.orchestrator = orchestrator
        self.progress_reader = progress_reader
        self.cancel_handler = cancel_handler
        self.validator = validator

    @staticmethod
    def _tenant(task: CodingTask) -> str:
        tenant_id = str(task.metadata.get("tenant_id", "")).strip()
        if not tenant_id:
            raise DomainOrchestratorError("AICode task tenant_id is required")
        return tenant_id

    async def plan(self, task: CodingTask) -> DomainPlan:
        """Return an explicit legacy pass-through plan; this is not a dedicated Planner agent."""
        return DomainPlan(
            tenant_id=self._tenant(task),
            domain=self.domain,
            task_id=task.task_id,
            objective=task.objective,
            acceptance_criteria=tuple(task.acceptance_criteria),
            execution_mode="legacy_aicode_passthrough",
        )

    async def dispatch(self, task: CodingTask) -> TaskRecord:
        self._tenant(task)
        return await self.orchestrator.run(task)

    async def progress(self, tenant_id: str, task_id: str) -> Mapping[str, Any]:
        if self.progress_reader is None:
            raise DomainOrchestratorError("AICode progress reader is not configured")
        result = await self.progress_reader(tenant_id, task_id)
        if not isinstance(result, Mapping):
            raise DomainOrchestratorError("progress reader must return an object")
        return result

    async def cancel(self, tenant_id: str, task_id: str) -> bool:
        if self.cancel_handler is None:
            raise DomainOrchestratorError("AICode cancellation handler is not configured")
        return bool(await self.cancel_handler(tenant_id, task_id))

    async def validate(self, record: TaskRecord) -> Mapping[str, Any]:
        if self.validator is not None:
            result = await self.validator(record)
            if not isinstance(result, Mapping):
                raise DomainOrchestratorError("validator must return an object")
            return result
        verifier = getattr(self.orchestrator, "_verify_evidence", None)
        if not callable(verifier):
            raise DomainOrchestratorError("AICode evidence verifier is unavailable")
        verdict = verifier(record)
        return {
            "verdict": "passed" if verdict is True else "hold" if verdict is None else "failed",
            "evidence_source": "legacy_aicode_evidence_gate",
            "artifact_digest_bound": False,
        }

    async def final_report(self, record: TaskRecord) -> Mapping[str, Any]:
        return {
            "task_id": record.task.task_id,
            "tenant_id": self._tenant(record.task),
            "domain": self.domain,
            "status": record.state.value,
            "objective": record.task.objective,
            "worker_summaries": [
                {
                    "role": item.role.value,
                    "success": item.success,
                    "summary": item.summary,
                    "artifacts": list(item.artifacts),
                    "blockers": list(item.blockers),
                }
                for item in record.results
            ],
            "errors": list(record.errors),
            "updated_at": record.updated_at,
        }
