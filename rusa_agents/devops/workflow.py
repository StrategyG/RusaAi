"""Safe, deterministic dummy workflow for the Agentic DevOps Phase 1 exit criterion."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .contracts import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentTask,
    AgentTaskStatus,
    ArtifactReference,
    EventType,
    TaskAcceptedPayload,
    TaskCompletedPayload,
    TaskRequestPayload,
    WorkflowEvent,
)


@dataclass(frozen=True)
class WorkflowExecution:
    """Validated result and ordered lifecycle events from one dummy workflow run."""

    result: AgentResult
    events: tuple[WorkflowEvent, ...]


class DummyWorkflowRunner:
    """Run one no-side-effect specialist task to prove contract-to-result handoff."""

    def __init__(self, identity: AgentIdentity) -> None:
        if identity.kind != AgentKind.SPECIALIST or identity.stage is None:
            raise ValueError("dummy workflow requires a stage-scoped specialist identity")
        if identity.allowed_tools:
            raise ValueError("dummy workflow identity must not declare executable tools")
        self.identity = identity

    def _event(
        self,
        task: AgentTask,
        event_type: EventType,
        producer: str,
        payload: dict,
        causation_id: str | None,
    ) -> WorkflowEvent:
        return WorkflowEvent(
            schema_version=1,
            type=event_type,
            message_id=str(uuid4()),
            tenant_id=task.tenant_id,
            run_id=task.workflow_id,
            task_id=task.task_id,
            attempt=task.attempt,
            correlation_id=task.correlation_id,
            causation_id=causation_id,
            traceparent=task.traceparent,
            producer=producer,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
        )

    def run(self, task: AgentTask) -> WorkflowExecution:
        """Validate, accept, and complete a task without tools, network, or writes."""
        if task.agent_id != self.identity.agent_id or task.stage != self.identity.stage:
            raise ValueError("task recipient and stage must match the registered dummy identity")
        if (
            task.constraints.allowed_tools
            or task.constraints.network_access
            or task.constraints.write_scope
            or task.constraints.approval_required
        ):
            raise ValueError("dummy workflow accepts only zero-side-effect task constraints")
        if not task.input_refs:
            raise ValueError("dummy workflow requires an immutable input artifact reference")
        started_at = datetime.now(timezone.utc)
        if task.deadline_at <= started_at:
            raise ValueError("task deadline has expired")

        request_event = self._event(
            task,
            EventType.TASK_REQUEST,
            task.source_agent,
            TaskRequestPayload(
                capability=task.stage.value,
                input_ref=task.input_refs[0].uri,
                deadline_at=task.deadline_at,
                idempotency_key=task.idempotency_key,
                priority=task.priority,
            ).model_dump(mode="json"),
            task.causation_id,
        )
        accepted_event = self._event(
            task,
            EventType.TASK_ACCEPTED,
            self.identity.agent_id,
            TaskAcceptedPayload(
                worker_id=self.identity.agent_id,
                lease_epoch=task.attempt,
                state_version=1,
            ).model_dump(mode="json"),
            request_event.message_id,
        )

        evidence_payload = json.dumps(
            {
                "task_id": task.task_id,
                "workflow_id": task.workflow_id,
                "attempt": task.attempt,
                "stage": task.stage.value,
                "status": AgentTaskStatus.PASSED.value,
                "acceptance_criteria": task.acceptance_criteria,
                "execution_mode": "dummy-no-side-effects",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        evidence = ArtifactReference(
            uri=(
                f"asset://rusa/dummy-evidence/{task.workflow_id}/"
                f"{task.task_id}/attempt-{task.attempt}"
            ),
            sha256=hashlib.sha256(evidence_payload).hexdigest(),
            media_type="application/json",
        )
        completed_at = datetime.now(timezone.utc)
        result = AgentResult(
            schema_version=1,
            task_id=task.task_id,
            workflow_id=task.workflow_id,
            tenant_id=task.tenant_id,
            agent_id=self.identity.agent_id,
            stage=self.identity.stage,
            status=AgentTaskStatus.PASSED,
            attempt=task.attempt,
            summary="Dummy specialist completed the contract workflow without side effects.",
            evidence=[evidence],
            started_at=started_at,
            completed_at=completed_at,
            model_id="dummy/no-model",
            policy_version="phase-1-no-side-effects-v1",
        )
        completed_event = self._event(
            task,
            EventType.TASK_COMPLETED,
            self.identity.agent_id,
            TaskCompletedPayload(
                lease_epoch=task.attempt,
                state_version=2,
                result_ref=evidence.uri,
                sha256=evidence.sha256,
            ).model_dump(mode="json"),
            accepted_event.message_id,
        )
        return WorkflowExecution(
            result=result,
            events=(request_event, accepted_event, completed_event),
        )
