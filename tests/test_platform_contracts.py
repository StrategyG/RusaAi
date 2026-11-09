from copy import deepcopy
from uuid import uuid4

import pytest

from rusa_agents.platform_contracts import (
    CoreAgentRole,
    DevOpsStage,
    PlatformContractError,
    PlatformDomain,
    validate_agent_result,
    validate_agent_task,
    validate_run_event,
    validate_run_event_batch,
)


def _task():
    return {
        "schema_version": "1.0",
        "task_id": str(uuid4()),
        "run_id": str(uuid4()),
        "tenant_id": "tenant-a",
        "idempotency_key": "run-task-00000001",
        "correlation_id": str(uuid4()),
        "created_at": "2026-11-09T10:00:00Z",
        "deadline_at": "2026-11-09T11:00:00Z",
        "domain": "autonomous_software_engineering",
        "stage": "code",
        "agent": {
            "agent_id": "software-engineer-1",
            "role": "software_engineer",
            "capabilities": ["repo_read", "workspace_write"],
        },
        "action": "repository.change",
        "input": {"objective": "Add a bounded platform contract"},
        "policy": {
            "policy_version": "1.0",
            "authorization_id": str(uuid4()),
            "decision": "allow",
            "approval_actions": ["merge", "deploy"],
        },
    }


def _result():
    return {
        "schema_version": "1.0",
        "task_id": str(uuid4()),
        "run_id": str(uuid4()),
        "tenant_id": "tenant-a",
        "correlation_id": str(uuid4()),
        "agent_id": "qa-agent-1",
        "status": "succeeded",
        "started_at": "2026-11-09T10:00:00Z",
        "completed_at": "2026-11-09T10:01:00Z",
        "artifacts": [{
            "kind": "test_report",
            "uri": "https://ci.example.test/artifacts/tests.json",
            "sha256": "a" * 64,
        }],
        "evidence": [{
            "kind": "unit_tests",
            "status": "pass",
            "subject_sha256": "b" * 64,
            "sha256": "c" * 64,
            "observed_at": "2026-11-09T10:01:00Z",
            "uri": "https://ci.example.test/artifacts/tests.json",
        }],
    }


def _event(sequence=1, state_version=1):
    return {
        "schema_version": "1.0",
        "event_id": str(uuid4()),
        "run_id": "2f1a0000-0000-4000-8000-000000000001",
        "tenant_id": "tenant-a",
        "sequence": sequence,
        "state_version": state_version,
        "event_type": "run.started",
        "occurred_at": "2026-11-09T10:00:00Z",
        "correlation_id": "2f1a0000-0000-4000-8000-000000000002",
        "actor": {"actor_id": "orchestrator-1", "actor_type": "agent", "agent_role": "supreme_orchestrator"},
        "payload": {"workflow": "coding"},
    }


def test_agent_task_contract_accepts_authorized_versioned_task():
    validate_agent_task(_task())
    assert len(PlatformDomain) == 4
    assert len(CoreAgentRole) == 10
    assert len(DevOpsStage) == 8


def test_agent_task_contract_rejects_unknown_domain_and_policy_decision():
    task = _task()
    task["domain"] = "unknown_domain"
    with pytest.raises(PlatformContractError):
        validate_agent_task(task)
    task = _task()
    task["policy"]["decision"] = "deny"
    with pytest.raises(PlatformContractError):
        validate_agent_task(task)


def test_agent_task_contract_rejects_deadline_without_forward_time():
    task = _task()
    task["deadline_at"] = task["created_at"]
    with pytest.raises(PlatformContractError, match="deadline_at"):
        validate_agent_task(task)


def test_agent_result_contract_requires_success_evidence_to_pass():
    result = _result()
    validate_agent_result(result)
    result["evidence"][0]["status"] = "hold"
    with pytest.raises(PlatformContractError, match="succeeded result"):
        validate_agent_result(result)


def test_agent_result_contract_rejects_inverted_execution_interval():
    result = _result()
    result["completed_at"] = "2026-11-09T09:59:00Z"
    with pytest.raises(PlatformContractError, match="completed_at"):
        validate_agent_result(result)


def test_succeeded_agent_result_requires_non_empty_passing_evidence():
    result = _result()
    result["evidence"] = []
    with pytest.raises(PlatformContractError, match="mandatory verification evidence"):
        validate_agent_result(result)


def test_failed_agent_result_requires_a_structured_failure_reason():
    result = _result()
    result["status"] = "failed"
    result["evidence"][0]["status"] = "fail"
    with pytest.raises(PlatformContractError, match="missing required field.*error"):
        validate_agent_result(result)
    result["error"] = {"code": "TEST_FAILURE", "message": "A required test failed"}
    validate_agent_result(result)


def test_contract_validation_diagnostics_never_echo_invalid_secret_values():
    task = _task()
    secret = "ghp_" + "A" * 36
    task["task_id"] = secret
    with pytest.raises(PlatformContractError) as exc_info:
        validate_agent_task(task)
    assert secret not in str(exc_info.value)

    task = _task()
    task["domain"] = secret
    with pytest.raises(PlatformContractError) as exc_info:
        validate_agent_task(task)
    assert secret not in str(exc_info.value)


def test_run_event_requires_agent_role_only_for_agent_actors():
    event = _event()
    event["actor"].pop("agent_role")
    with pytest.raises(PlatformContractError, match="agent actors"):
        validate_run_event(event)

    event = _event()
    event["actor"]["actor_type"] = "human"
    with pytest.raises(PlatformContractError, match="only agent actors"):
        validate_run_event(event)


@pytest.mark.parametrize(
    "event_type",
    [
        "plan.completed",
        "code.completed",
        "build.completed",
        "test.completed",
        "release.approved",
        "deploy.completed",
        "operate.incident_detected",
        "operate.recovered",
        "monitor.alert_triggered",
        "monitor.feedback_created",
        "workflow.blocked",
        "workflow.completed",
    ],
)
def test_devops_lifecycle_event_vocabulary_is_supported(event_type):
    event = _event()
    event["event_type"] = event_type
    validate_run_event(event)


def test_run_event_contract_and_monotonic_batch():
    first = _event(1, 1)
    second = _event(2, 2)
    second["run_id"] = first["run_id"]
    second["tenant_id"] = first["tenant_id"]
    second["correlation_id"] = first["correlation_id"]
    validate_run_event(first)
    validate_run_event_batch([first, second])


def test_run_event_batch_rejects_cross_tenant_and_non_monotonic_state():
    first = _event(1, 2)
    second = _event(2, 1)
    second["run_id"] = first["run_id"]
    second["tenant_id"] = "tenant-b"
    second["correlation_id"] = first["correlation_id"]
    with pytest.raises(PlatformContractError, match="tenant IDs"):
        validate_run_event_batch([first, second])

    second["tenant_id"] = first["tenant_id"]
    with pytest.raises(PlatformContractError, match="state_version"):
        validate_run_event_batch([first, second])


def test_run_event_batch_rejects_duplicate_and_out_of_order_sequence():
    first = _event(2, 2)
    second = deepcopy(first)
    second["event_id"] = str(uuid4())
    second["sequence"] = 1
    with pytest.raises(PlatformContractError, match="strictly increasing"):
        validate_run_event_batch([first, second])


def test_run_event_batch_rejects_duplicate_event_ids():
    first = _event(1, 1)
    second = deepcopy(first)
    second["sequence"] = 2
    with pytest.raises(PlatformContractError, match="unique"):
        validate_run_event_batch([first, second])
