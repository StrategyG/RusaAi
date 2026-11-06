from __future__ import annotations

from uuid import uuid4

import pytest

from rusa_agents.devops.contracts import (
    ContractValidationError,
    validate_agent_result,
    validate_agent_task,
    validate_workflow_event,
)


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _task() -> dict:
    return {
        "task_id": str(uuid4()),
        "workflow_id": str(uuid4()),
        "tenant_id": "tenant-demo",
        "agent_id": "rusa_plan_agent",
        "stage": "plan",
        "objective": "Turn the approved request into an implementation plan.",
        "input": {"user_request": "Prepare a bounded release plan."},
        "constraints": {
            "allowed_tools": ["repository.read", "issue.read"],
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": ["plans/"],
            "approval_required": False,
            "budget": {
                "max_cost_usd": 2.0,
                "max_tool_calls": 20,
                "max_runtime_seconds": 300,
            },
        },
        "attempt": 1,
        "audit": {
            "created_at": "2026-11-07T00:00:00Z",
            "requested_by": "tenant-admin",
            "policy_version": "policy-1",
            "traceparent": TRACEPARENT,
            "model_id": "provider/model-version",
        },
    }


def _result() -> dict:
    return {
        "task_id": str(uuid4()),
        "workflow_id": str(uuid4()),
        "tenant_id": "tenant-demo",
        "agent_id": "rusa_test_agent",
        "stage": "test",
        "status": "passed",
        "output": {
            "summary": "Required test evidence is available.",
            "artifact_refs": ["artifact://build/sha256"],
            "evidence_refs": ["evidence://ci/test-run-1"],
            "findings": [],
        },
        "audit": {
            "started_at": "2026-11-07T00:00:00Z",
            "completed_at": "2026-11-07T00:00:30Z",
            "policy_version": "policy-1",
            "source_commit": "a" * 40,
            "artifact_digest": "sha256:" + "b" * 64,
        },
    }


def _event() -> dict:
    return {
        "schema_version": 1,
        "type": "task.progress",
        "message_id": "msg-123",
        "tenant_id": "tenant-demo",
        "run_id": "run-123",
        "task_id": "task-123",
        "attempt": 1,
        "correlation_id": "corr-123",
        "traceparent": TRACEPARENT,
        "producer": "rusa_test_agent",
        "occurred_at": "2026-11-07T00:00:15Z",
        "payload": {
            "lease_epoch": 1,
            "state_version": 2,
            "percent": 50,
            "stage": "test",
        },
    }


def test_agent_task_contract_accepts_stage_specific_plan_task():
    validate_agent_task(_task())


def test_agent_task_contract_rejects_agent_stage_mismatch():
    payload = _task()
    payload["agent_id"] = "rusa_deploy_agent"
    with pytest.raises(ContractValidationError, match="agent_task"):
        validate_agent_task(payload)


def test_deploy_task_requires_approval_and_deploy_scope():
    payload = _task()
    payload.update(agent_id="rusa_deploy_agent", stage="deploy")
    payload["input"] = {
        "release_manifest_ref": "artifact://release/manifest",
        "environment": "production",
    }
    payload["constraints"].update(
        approval_required=False,
        approval_scope=[],
    )
    with pytest.raises(ContractValidationError, match="approval_required"):
        validate_agent_task(payload)


def test_monitor_task_is_read_only_and_has_no_network_access():
    payload = _task()
    payload.update(agent_id="rusa_monitor_agent", stage="monitor")
    payload["input"] = {"service": "rusa-api", "metrics_window": "last-15m"}
    payload["constraints"].update(
        network_access=True,
        allowed_network_destinations=["metrics.internal"],
        write_scope=[],
    )
    with pytest.raises(ContractValidationError, match="network_access"):
        validate_agent_task(payload)


def test_agent_result_passed_requires_evidence():
    payload = _result()
    validate_agent_result(payload)
    payload["output"]["evidence_refs"] = []
    with pytest.raises(ContractValidationError, match="evidence_refs"):
        validate_agent_result(payload)


def test_agent_result_needs_approval_requires_approval_reference():
    payload = _result()
    payload["status"] = "needs_approval"
    with pytest.raises(ContractValidationError, match="approval_ref"):
        validate_agent_result(payload)


def test_workflow_event_contract_accepts_progress_event():
    validate_workflow_event(_event())


def test_workflow_event_contract_rejects_invalid_traceparent():
    payload = _event()
    payload["traceparent"] = "not-a-trace"
    with pytest.raises(ContractValidationError, match="traceparent"):
        validate_workflow_event(payload)


def test_workflow_event_contract_rejects_unknown_event_type():
    payload = _event()
    payload["type"] = "task.magic"
    with pytest.raises(ContractValidationError, match="type"):
        validate_workflow_event(payload)


def test_workflow_event_completed_requires_content_digest():
    payload = _event()
    payload["type"] = "task.completed"
    payload["payload"] = {
        "lease_epoch": 1,
        "state_version": 3,
        "result_ref": "artifact://result/123",
        "sha256": "bad-digest",
    }
    with pytest.raises(ContractValidationError, match="sha256"):
        validate_workflow_event(payload)


def test_contract_payload_must_be_an_object():
    with pytest.raises(ContractValidationError, match="JSON object"):
        validate_agent_task(["not", "an", "object"])


def test_packaged_contract_schemas_match_repository_sources():
    from pathlib import Path

    repository_dir = Path("schemas/devops")
    packaged_dir = Path("rusa_agents/devops/schemas")
    for filename in (
        "agent-task.schema.json",
        "agent-result.schema.json",
        "workflow-event.schema.json",
    ):
        assert (packaged_dir / filename).read_bytes() == (
            repository_dir / filename
        ).read_bytes()
