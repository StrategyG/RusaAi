"""End-to-end proof that Phase 1 contracts support a safe dummy workflow."""

from datetime import datetime, timedelta, timezone

import pytest

from rusa_agents.devops.contracts import (
    AgentConstraints,
    AgentIdentity,
    AgentKind,
    AgentStage,
    AgentTask,
    AgentTaskStatus,
    EventType,
)
from rusa_agents.devops.workflow import DummyWorkflowRunner

TRACEPARENT = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"


def make_identity(**overrides):
    values = {
        "agent_id": "rusa_test_agent",
        "display_name": "RUSA Dummy Test Agent",
        "kind": AgentKind.SPECIALIST,
        "stage": AgentStage.TEST,
        "version": "1.0.0",
        "capabilities": ["dummy.validate"],
        "allowed_tools": [],
        "policy_scopes": ["workflow.dummy"],
    }
    values.update(overrides)
    return AgentIdentity.model_validate(values)


def make_task(**overrides):
    values = {
        "schema_version": 1,
        "task_id": "dummy-task-001",
        "workflow_id": "dummy-workflow-001",
        "tenant_id": "tenant-test",
        "source_agent": "rusa_devops_orchestrator",
        "agent_id": "rusa_test_agent",
        "stage": "test",
        "objective": "Prove the lifecycle contract using a no-side-effect dummy agent",
        "input_refs": [{
            "uri": "asset://rusa/dummy-input/commit",
            "sha256": "a" * 64,
            "media_type": "application/json",
        }],
        "context": {"execution_mode": "dummy"},
        "constraints": {
            "allowed_tools": [],
            "network_access": False,
            "write_scope": [],
            "approval_required": False,
        },
        "priority": "P2",
        "acceptance_criteria": ["The dummy agent returns a validated result"],
        "deadline_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "attempt": 1,
        "idempotency_key": "dummy-workflow-001.task-001.1",
        "correlation_id": "dummy-correlation-001",
        "traceparent": TRACEPARENT,
    }
    values.update(overrides)
    return AgentTask.model_validate(values)


def test_dummy_workflow_completes_request_acceptance_and_result_handoff():
    execution = DummyWorkflowRunner(make_identity()).run(make_task())

    assert execution.result.status == AgentTaskStatus.PASSED
    assert execution.result.attempt == 1
    assert len(execution.result.evidence) == 1
    assert len(execution.result.evidence[0].sha256) == 64
    assert [event.type for event in execution.events] == [
        EventType.TASK_REQUEST,
        EventType.TASK_ACCEPTED,
        EventType.TASK_COMPLETED,
    ]
    assert execution.events[1].causation_id == execution.events[0].message_id
    assert execution.events[2].causation_id == execution.events[1].message_id
    assert execution.events[2].payload["sha256"] == execution.result.evidence[0].sha256
    assert all(event.tenant_id == "tenant-test" for event in execution.events)


@pytest.mark.parametrize(
    "constraints",
    [
        AgentConstraints(allowed_tools=["write_file"]),
        AgentConstraints(network_access=True),
        AgentConstraints(write_scope=["src/"]),
        AgentConstraints(approval_required=True),
    ],
)
def test_dummy_workflow_rejects_any_execution_capability(constraints):
    task = make_task(constraints=constraints)
    with pytest.raises(ValueError, match="zero-side-effect"):
        DummyWorkflowRunner(make_identity()).run(task)


def test_dummy_workflow_rejects_identity_or_stage_mismatch():
    runner = DummyWorkflowRunner(make_identity())
    with pytest.raises(ValueError, match="recipient and stage"):
        runner.run(make_task(agent_id="rusa_plan_agent"))


def test_dummy_workflow_rejects_expired_tasks():
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="deadline has expired"):
        DummyWorkflowRunner(make_identity()).run(make_task(deadline_at=expired))


def test_dummy_workflow_identity_cannot_declare_tools():
    with pytest.raises(ValueError, match="must not declare executable tools"):
        DummyWorkflowRunner(make_identity(allowed_tools=["run_check"]))
