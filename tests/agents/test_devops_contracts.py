from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from rusa_agents.contracts import TaskState
from rusa_agents.devops.contracts import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentStage,
    AgentTask,
    AgentTaskStatus,
    EventType,
    WorkflowEvent,
    map_aicode_task_state,
)

TRACEPARENT = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
ARTIFACT = {
    "uri": "asset://rusa/test-report-v1",
    "sha256": "a" * 64,
    "media_type": "application/json",
}


def task_payload():
    return {
        "schema_version": 1,
        "task_id": "task-001",
        "workflow_id": "workflow-001",
        "tenant_id": "tenant-a",
        "source_agent": "rusa_devops_orchestrator",
        "agent_id": "rusa_test_agent",
        "stage": "test",
        "objective": "Verify the candidate commit against acceptance criteria",
        "input_refs": [ARTIFACT],
        "context": {"repository": "StrategyG/RusaAi", "commit_sha": "b" * 40},
        "constraints": {
            "allowed_tools": ["pytest", "ruff"],
            "network_access": False,
            "write_scope": ["test-reports/"],
            "approval_required": False,
        },
        "priority": "P2",
        "acceptance_criteria": ["All required tests pass", "Evidence references the tested commit"],
        "deadline_at": "2026-11-11T12:00:00Z",
        "attempt": 1,
        "idempotency_key": "workflow-001/task-001/1",
        "correlation_id": "correlation-001",
        "traceparent": TRACEPARENT,
        "created_at": "2026-11-10T12:00:00Z",
    }


def event_payload(**overrides):
    payload = {
        "schema_version": 1,
        "type": "task.request",
        "message_id": "message-001",
        "tenant_id": "tenant-a",
        "run_id": "workflow-001",
        "task_id": "task-001",
        "attempt": 1,
        "correlation_id": "correlation-001",
        "traceparent": TRACEPARENT,
        "producer": "rusa_devops_orchestrator",
        "occurred_at": "2026-11-10T12:00:00Z",
        "payload": {
            "capability": "test",
            "input_ref": "asset://rusa/commit-b",
            "deadline_at": "2026-11-11T12:00:00Z",
            "idempotency_key": "workflow-001/task-001/1",
            "priority": "P2",
        },
    }
    payload.update(overrides)
    return payload


def test_agent_task_accepts_lifecycle_contract_and_artifact_digest():
    task = AgentTask.model_validate(task_payload())

    assert task.schema_version == 1
    assert task.stage == AgentStage.TEST
    assert task.agent_id == "rusa_test_agent"
    assert task.input_refs[0].sha256 == "a" * 64
    assert task.constraints.network_access is False


def test_agent_task_rejects_missing_acceptance_criteria_and_bad_trace_context():
    payload = task_payload()
    payload["acceptance_criteria"] = []
    with pytest.raises(ValidationError):
        AgentTask.model_validate(payload)

    payload = task_payload()
    payload["traceparent"] = "not-a-w3c-traceparent"
    with pytest.raises(ValidationError):
        AgentTask.model_validate(payload)


def test_agent_task_rejects_write_scope_path_traversal():
    payload = task_payload()
    payload["constraints"]["write_scope"] = ["test-reports/../../secrets"]
    with pytest.raises(ValidationError, match="without traversal"):
        AgentTask.model_validate(payload)


def test_agent_identity_requires_stage_only_for_specialists():
    specialist = AgentIdentity(
        agent_id="rusa_test_agent",
        display_name="RUSA Test Agent",
        kind=AgentKind.SPECIALIST,
        stage=AgentStage.TEST,
        capabilities=["test"],
        allowed_tools=["pytest"],
    )
    orchestrator = AgentIdentity(
        agent_id="rusa_devops_orchestrator",
        display_name="RUSA DevOps Orchestrator",
        kind=AgentKind.ORCHESTRATOR,
    )

    assert specialist.stage == AgentStage.TEST
    assert orchestrator.stage is None

    with pytest.raises(ValidationError, match="must declare one lifecycle stage"):
        AgentIdentity(
            agent_id="rusa_code_agent",
            display_name="RUSA Code Agent",
            kind=AgentKind.SPECIALIST,
        )


def test_passed_agent_result_requires_evidence_and_completion_time():
    result = AgentResult(
        schema_version=1,
        task_id="task-001",
        workflow_id="workflow-001",
        tenant_id="tenant-a",
        agent_id="rusa_test_agent",
        stage="test",
        status="passed",
        summary="Required checks passed for the candidate commit.",
        evidence=[ARTIFACT],
        started_at=NOW,
        completed_at=datetime(2026, 11, 10, 12, 5, tzinfo=timezone.utc),
        policy_version="policy-1",
    )

    assert result.status == AgentTaskStatus.PASSED
    assert result.evidence[0].sha256 == "a" * 64

    with pytest.raises(ValidationError, match="must include evidence"):
        AgentResult(
            schema_version=1,
            task_id="task-002",
            workflow_id="workflow-001",
            tenant_id="tenant-a",
            agent_id="rusa_test_agent",
            stage="test",
            status="passed",
            attempt=1,
            summary="Checks passed without an evidence reference.",
            completed_at=NOW,
        )


def test_terminal_result_requires_completed_at_and_monotonic_timestamps():
    base = {
        "schema_version": 1,
        "task_id": "task-003",
        "workflow_id": "workflow-001",
        "tenant_id": "tenant-a",
        "agent_id": "rusa_code_agent",
        "stage": "code",
        "status": "failed",
        "attempt": 1,
        "summary": "Implementation was blocked.",
    }
    with pytest.raises(ValidationError, match="completed_at"):
        AgentResult.model_validate(base)

    with pytest.raises(ValidationError, match="cannot precede"):
        AgentResult.model_validate({
            **base,
            "started_at": "2026-11-10T12:05:00Z",
            "completed_at": "2026-11-10T12:00:00Z",
        })


def test_workflow_event_validates_event_specific_payload():
    event = WorkflowEvent.model_validate(event_payload())

    assert event.type == EventType.TASK_REQUEST
    assert event.payload["capability"] == "test"

    invalid = event_payload(payload={"capability": "test"})
    with pytest.raises(ValidationError):
        WorkflowEvent.model_validate(invalid)


def test_task_lifecycle_event_requires_task_id_and_attempt():
    payload = event_payload(task_id=None, attempt=None)
    with pytest.raises(ValidationError, match="require task_id and attempt"):
        WorkflowEvent.model_validate(payload)


def test_policy_violation_event_can_be_workflow_scoped():
    event = WorkflowEvent.model_validate(event_payload(
        type="policy.violation",
        task_id=None,
        attempt=None,
        payload={
            "policy_id": "tool-boundary",
            "policy_version": "1.0.0",
            "decision_id": "decision-001",
            "action": "production.deploy",
            "resource_ref": "environment://production",
            "reason_code": "approval_required",
        },
    ))

    assert event.type == EventType.POLICY_VIOLATION
    assert event.task_id is None


def test_legacy_aicode_states_map_to_agentic_devops_vocabulary():
    assert map_aicode_task_state(TaskState.RECEIVED) == AgentTaskStatus.QUEUED
    assert map_aicode_task_state(TaskState.EXECUTING) == AgentTaskStatus.RUNNING
    assert map_aicode_task_state(TaskState.AWAITING_APPROVAL) == AgentTaskStatus.NEEDS_APPROVAL
    assert map_aicode_task_state(TaskState.SUCCEEDED) == AgentTaskStatus.PASSED
    assert map_aicode_task_state("cancelled") == AgentTaskStatus.CANCELLED

    with pytest.raises(ValueError, match="unsupported legacy"):
        map_aicode_task_state("unknown")
