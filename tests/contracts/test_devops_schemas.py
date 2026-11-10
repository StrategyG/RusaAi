import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from rusa_agents.devops.contracts import (
    AgentIdentity,
    AgentKind,
    AgentResult,
    AgentTask,
    WorkflowEvent,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas" / "devops"
TRACEPARENT = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
ARTIFACT = {
    "uri": "asset://rusa/evidence-v1",
    "sha256": "a" * 64,
    "media_type": "application/json",
}


def _validator(name):
    schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_agent_task_json_schema_matches_typed_model():
    task = AgentTask.model_validate({
        "schema_version": 1,
        "task_id": "task-schema",
        "workflow_id": "workflow-schema",
        "tenant_id": "tenant-a",
        "source_agent": "rusa_devops_orchestrator",
        "agent_id": "rusa_plan_agent",
        "stage": "plan",
        "objective": "Create a delivery plan",
        "acceptance_criteria": ["Plan includes test and security gates"],
        "deadline_at": "2026-11-11T12:00:00Z",
        "idempotency_key": "workflow-schema/task-schema/1",
        "correlation_id": "corr-schema",
        "traceparent": TRACEPARENT,
    })
    errors = list(_validator("agent-task.schema.json").iter_errors(task.model_dump(mode="json")))
    assert errors == []


def test_agent_result_json_schema_matches_typed_model():
    result = AgentResult.model_validate({
        "schema_version": 1,
        "task_id": "task-result",
        "workflow_id": "workflow-schema",
        "tenant_id": "tenant-a",
        "agent_id": "rusa_test_agent",
        "stage": "test",
        "status": "passed",
        "summary": "All required tests passed.",
        "evidence": [ARTIFACT],
        "completed_at": "2026-11-10T12:05:00Z",
    })
    errors = list(_validator("agent-result.schema.json").iter_errors(result.model_dump(mode="json")))
    assert errors == []


def test_agent_identity_json_schema_matches_specialist_and_orchestrator():
    validator = _validator("agent-identity.schema.json")
    specialist = AgentIdentity(
        agent_id="rusa_test_agent",
        display_name="RUSA Test Agent",
        kind="specialist",
        stage="test",
        allowed_tools=["pytest"],
    )
    orchestrator = AgentIdentity(
        agent_id="rusa_devops_orchestrator",
        display_name="RUSA DevOps Orchestrator",
        kind=AgentKind.ORCHESTRATOR,
    )

    assert list(validator.iter_errors(specialist.model_dump(mode="json"))) == []
    assert list(validator.iter_errors(orchestrator.model_dump(mode="json"))) == []


def test_workflow_event_json_schema_matches_typed_model():
    event = WorkflowEvent.model_validate({
        "schema_version": 1,
        "type": "task.completed",
        "message_id": "message-schema",
        "tenant_id": "tenant-a",
        "run_id": "workflow-schema",
        "task_id": "task-schema",
        "attempt": 1,
        "correlation_id": "corr-schema",
        "traceparent": TRACEPARENT,
        "producer": "rusa_test_agent",
        "occurred_at": "2026-11-10T12:05:00Z",
        "payload": {
            "lease_epoch": 2,
            "state_version": 7,
            "result_ref": ARTIFACT["uri"],
            "sha256": ARTIFACT["sha256"],
        },
    })
    errors = list(_validator("workflow-event.schema.json").iter_errors(event.model_dump(mode="json")))
    assert errors == []
