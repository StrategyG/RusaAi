"""Compatibility adapters between legacy AICode dataclasses and platform v2 contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .contracts import CodingTask, RepositoryTarget, WorkerResult, WorkerRole
from .platform_contracts import (
    AgentDomain,
    AgentResultV2,
    ExecutionBudget,
    TaskEnvelopeV2,
    TaskOutcome,
    utc_now,
)


def _legacy_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("legacy CodingTask.created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("legacy CodingTask.created_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def coding_task_to_envelope(
    task: CodingTask,
    *,
    deadline_at: datetime | None = None,
    budget: ExecutionBudget | None = None,
    attempt: int = 1,
    priority: int = 5,
    workflow_id: str | None = None,
    run_id: str | None = None,
    agent_id: str = "aicode.legacy_adapter",
    agent_version: str = "1.0.0",
    capability: str = "coding.execute",
) -> TaskEnvelopeV2:
    """Adapt a current CodingTask without copying arbitrary metadata or secrets.

    The one-hour/100-tool-call defaults are migration bounds, not a replacement
    for a future tenant policy or an agent manifest budget.
    """
    tenant_id = str(task.metadata.get("tenant_id", "default")).strip()
    if not tenant_id:
        raise ValueError("task tenant_id cannot be empty")

    effective_budget = budget or ExecutionBudget()
    created_at = _legacy_timestamp(task.created_at)
    effective_deadline = deadline_at or (
        utc_now() + timedelta(seconds=effective_budget.max_execution_seconds)
    )
    return TaskEnvelopeV2(
        tenant_id=tenant_id,
        workflow_id=workflow_id or task.task_id,
        run_id=run_id or task.task_id,
        task_id=task.task_id,
        correlation_id=task.task_id,
        domain=AgentDomain.SOFTWARE_ENGINEERING,
        agent_id=agent_id,
        agent_version=agent_version,
        capability=capability,
        attempt=attempt,
        priority=priority,
        deadline_at=effective_deadline,
        budget=effective_budget,
        input={
            "objective": task.objective,
            "acceptance_criteria": list(task.acceptance_criteria),
            "context": task.context,
            "repository": {
                "full_name": task.repository.full_name,
                "base_branch": task.repository.base_branch,
                "work_branch": task.repository.work_branch,
            },
        },
        policy_context={"adapter": "aicode-legacy-v1"},
        created_at=created_at,
    )


def task_envelope_to_coding_task(envelope: TaskEnvelopeV2) -> CodingTask:
    """Convert a Software Engineering coding envelope to the legacy task type."""
    if envelope.domain != AgentDomain.SOFTWARE_ENGINEERING:
        raise ValueError("only software_engineering tasks can be adapted to CodingTask")
    if envelope.capability != "coding.execute":
        raise ValueError("task capability is not supported by the AICode adapter")

    payload = envelope.input
    objective = payload.get("objective")
    criteria = payload.get("acceptance_criteria")
    repository = payload.get("repository")
    if not isinstance(objective, str) or not isinstance(criteria, list):
        raise ValueError("coding task input requires objective and acceptance_criteria")
    if not all(isinstance(item, str) for item in criteria):
        raise ValueError("acceptance_criteria must contain only strings")
    if not isinstance(repository, dict):
        raise ValueError("coding task input requires a repository object")

    full_name = repository.get("full_name")
    base_branch = repository.get("base_branch", "main")
    work_branch = repository.get("work_branch", "feature/aicode-task")
    if not all(isinstance(item, str) for item in (full_name, base_branch, work_branch)):
        raise ValueError("repository full_name and branch values must be strings")

    context = payload.get("context", "")
    if not isinstance(context, str):
        raise ValueError("coding task context must be a string")

    return CodingTask(
        objective=objective,
        repository=RepositoryTarget(
            full_name=full_name,
            base_branch=base_branch,
            work_branch=work_branch,
        ),
        acceptance_criteria=list(criteria),
        context=context,
        task_id=envelope.task_id,
        created_at=envelope.created_at.isoformat(),
        metadata={"tenant_id": envelope.tenant_id},
    )


def worker_result_to_agent_result(
    result: WorkerResult,
    envelope: TaskEnvelopeV2,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    agent_version: str = "1.0.0",
) -> AgentResultV2:
    """Adapt a WorkerResult while clearly labelling legacy evidence as unverified."""
    started = started_at or completed_at or utc_now()
    completed = completed_at or max(utc_now(), started)
    if result.success and not result.blockers:
        status = TaskOutcome.SUCCEEDED
    elif result.blockers:
        status = TaskOutcome.BLOCKED
    else:
        status = TaskOutcome.FAILED

    return AgentResultV2(
        tenant_id=envelope.tenant_id,
        workflow_id=envelope.workflow_id,
        run_id=envelope.run_id,
        task_id=envelope.task_id,
        agent_id=f"aicode.worker.{result.role.value}",
        agent_version=agent_version,
        capability=f"coding.{result.role.value}",
        attempt=envelope.attempt,
        status=status,
        summary=result.summary or f"{result.role.value} worker returned no summary",
        output={"artifacts": list(result.artifacts)},
        legacy_evidence=dict(result.evidence),
        blockers=list(result.blockers),
        started_at=started,
        completed_at=completed,
    )


def agent_result_to_worker_result(
    result: AgentResultV2,
    *,
    role: WorkerRole,
) -> WorkerResult:
    """Adapt a v2 result back to the current WorkerResult contract."""
    artifacts = result.output.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(item, str) for item in artifacts):
        artifacts = []

    success = result.status == TaskOutcome.SUCCEEDED and not result.blockers
    blockers = list(result.blockers)
    if not success and not blockers:
        blockers.append(result.error or f"agent result status: {result.status.value}")

    return WorkerResult(
        role=role,
        summary=result.summary,
        success=success,
        artifacts=list(artifacts),
        evidence=dict(result.legacy_evidence),
        blockers=blockers,
    )
