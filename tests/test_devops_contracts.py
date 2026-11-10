import pytest  # noqa: I001

from rusa_agents.devops.contracts import (
    ContractValidationError,
    validate_agent_result,
    validate_agent_result_for_task,
    validate_agent_task,
    validate_workflow_event,
)


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _task() -> dict:
    import uuid

    return {
        "schema_version": 1,
        "task_id": str(uuid.uuid4()),
        "workflow_id": str(uuid.uuid4()),
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
    import uuid

    return {
        "schema_version": 1,
        "task_id": str(uuid.uuid4()),
        "workflow_id": str(uuid.uuid4()),
        "tenant_id": "tenant-demo",
        "agent_id": "rusa_test_agent",
        "stage": "test",
        "attempt": 1,
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

def test_agent_task_contract_rejects_missing_schema_version():
    payload = _task()
    payload.pop("schema_version")
    with pytest.raises(ContractValidationError, match="schema_version"):
        validate_agent_task(payload)


def test_agent_task_contract_rejects_agent_stage_mismatch():
    payload = _task()
    payload["agent_id"] = "rusa_deploy_agent"
    with pytest.raises(ContractValidationError, match="agent_task"):
        validate_agent_task(payload)

def test_security_guard_is_cross_cutting_and_read_only():
    payload = _task()
    payload.update(agent_id="rusa_security_guard_agent", stage="deploy")
    payload["input"] = {
        "release_manifest_ref": "artifact://release/manifest",
        "environment": "staging",
        "commit_sha": "a" * 40,
        "artifact_digest": "sha256:" + "b" * 64,
        "evidence_refs": ["evidence://release/verification"],
    }
    payload["constraints"].update(
        approval_required=True,
        approval_scope=["deploy"],
        write_scope=[],
    )
    validate_agent_task(payload)


def test_deploy_task_requires_approval_and_deploy_scope():
    payload = _task()
    payload.update(agent_id="rusa_deploy_agent", stage="deploy")
    payload["input"] = {
        "release_manifest_ref": "artifact://release/manifest",
        "environment": "production",
        "commit_sha": "a" * 40,
        "artifact_digest": "sha256:" + "b" * 64,
    }
    payload["constraints"].update(
        allowed_tools=["artifact.read", "release.read", "deployment.execute"],
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
        allowed_tools=["metrics.read", "logs.read"],
        network_access=True,
        allowed_network_destinations=["metrics.internal"],
        write_scope=[],
    )
    with pytest.raises(ContractValidationError, match="network_access"):
        validate_agent_task(payload)


def test_agent_result_security_guard_can_review_any_lifecycle_stage():
    payload = _result()
    payload["agent_id"] = "rusa_security_guard_agent"
    validate_agent_result(payload)


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
        "stage": "test",
        "domain_event": "test.completed",
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


def test_agent_task_requires_complete_budget():
    payload = _task()
    payload["constraints"].pop("budget")
    with pytest.raises(ContractValidationError, match="budget"):
        validate_agent_task(payload)


def test_agent_task_rejects_capability_outside_agent_allowlist():
    payload = _task()
    payload["constraints"]["allowed_tools"] = ["repository.write"]
    with pytest.raises(ContractValidationError, match="allowed_tools"):
        validate_agent_task(payload)


def test_agent_task_rejects_path_traversal_in_write_scope():
    payload = _task()
    payload["constraints"]["write_scope"] = ["plans/../../secrets"]
    with pytest.raises(ContractValidationError, match="write_scope"):
        validate_agent_task(payload)


def test_production_deploy_requires_human_approval_reference():
    payload = _task()
    payload.update(agent_id="rusa_deploy_agent", stage="deploy")
    payload["input"] = {
        "release_manifest_ref": "artifact://release/manifest",
        "environment": "production",
        "commit_sha": "a" * 40,
        "artifact_digest": "sha256:" + "b" * 64,
    }
    payload["constraints"].update(
        allowed_tools=["artifact.read", "release.read", "deployment.execute"],
        approval_required=True,
        approval_scope=["deploy"],
        approval_ref="approval://change/123",
    )
    validate_agent_task(payload)
    payload["constraints"].pop("approval_ref")
    with pytest.raises(ContractValidationError, match="approval_ref"):
        validate_agent_task(payload)


def test_agent_result_passed_artifact_stage_requires_commit_and_digest():
    payload = _result()
    payload["audit"].pop("artifact_digest")
    with pytest.raises(ContractValidationError, match="artifact_digest"):
        validate_agent_result(payload)


def _matching_test_task_and_result() -> tuple[dict, dict]:
    task = _task()
    task.update(agent_id="rusa_test_agent", stage="test")
    task["input"] = {
        "commit_sha": "a" * 40,
        "artifact_digest": "sha256:" + "b" * 64,
        "build_evidence_ref": "evidence://build/evidence",
        "test_plan_ref": "artifact://test/plan",
        "acceptance_criteria_ref": "artifact://test/criteria",
    }
    task["constraints"].update(
        allowed_tools=["repository.read", "artifact.read", "test.execute", "evidence.write"],
        write_scope=["test-reports/"],
    )
    result = _result()
    result.update(task_id=task["task_id"], workflow_id=task["workflow_id"],
                  tenant_id=task["tenant_id"], agent_id=task["agent_id"], stage=task["stage"])
    return task, result


def test_agent_result_must_match_originating_task_and_artifact():
    task, result = _matching_test_task_and_result()
    validate_agent_result_for_task(task, result)
    result["audit"]["artifact_digest"] = "sha256:" + "c" * 64
    with pytest.raises(ContractValidationError, match="artifact_digest"):
        validate_agent_result_for_task(task, result)


def test_agent_result_must_match_originating_task_commit():
    task, result = _matching_test_task_and_result()
    result["audit"]["source_commit"] = "c" * 40
    with pytest.raises(ContractValidationError, match="source_commit"):
        validate_agent_result_for_task(task, result)


def test_task_completed_event_requires_stage_domain_mapping():
    payload = _event()
    payload["type"] = "task.completed"
    payload["payload"] = {
        "lease_epoch": 1, "state_version": 3,
        "result_ref": "artifact://result/123", "sha256": "a" * 64,
        "stage": "test", "domain_event": "test.completed",
    }
    validate_workflow_event(payload)
    payload["payload"]["domain_event"] = "deploy.completed"
    with pytest.raises(ContractValidationError, match="domain_event"):
        validate_workflow_event(payload)


def test_workflow_blocked_event_is_validated():
    payload = _event()
    payload.pop("task_id")
    payload.pop("attempt")
    payload["type"] = "workflow.blocked"
    payload["payload"] = {"state_version": 4, "stage": "release", "reason_code": "approval_required"}
    validate_workflow_event(payload)


def test_workflow_completed_event_requires_result_digest():
    payload = _event()
    payload.pop("task_id")
    payload.pop("attempt")
    payload["type"] = "workflow.completed"
    payload["payload"] = {"state_version": 8, "result_ref": "artifact://workflow/result", "sha256": "a" * 64}
    validate_workflow_event(payload)

def test_agent_task_requires_attempt_identity():
    payload = _task()
    payload.pop("attempt")
    with pytest.raises(ContractValidationError, match="attempt"):
        validate_agent_task(payload)


def test_agent_result_requires_attempt_identity():
    payload = _result()
    payload.pop("attempt")
    with pytest.raises(ContractValidationError, match="attempt"):
        validate_agent_result(payload)


def test_agent_result_must_match_originating_task_attempt():
    task, result = _matching_test_task_and_result()
    task["attempt"] = 2
    result["attempt"] = 1
    with pytest.raises(ContractValidationError, match="attempt"):
        validate_agent_result_for_task(task, result)

def test_release_task_requires_the_commit_that_produced_its_artifact():
    payload = _task()
    payload.update(agent_id="rusa_release_agent", stage="release")
    payload["input"] = {
        "commit_sha": "a" * 40,
        "artifact_digest": "sha256:" + "b" * 64,
        "build_evidence_ref": "evidence://build/attestation",
        "test_evidence_ref": "evidence://tests/verified",
        "sbom_ref": "artifact://sbom/verified",
        "release_version": "0.1.0-rc.1",
        "changelog_ref": "artifact://release/changelog",
        "migration_notes_ref": "artifact://release/migrations",
    }
    payload["constraints"]["allowed_tools"] = [
        "artifact.read", "evidence.read", "release.prepare", "manifest.write",
        "sbom.read", "evidence.write"
    ]
    validate_agent_task(payload)
    payload["input"].pop("commit_sha")
    with pytest.raises(ContractValidationError, match="commit_sha"):
        validate_agent_task(payload)


def test_deploy_task_requires_source_commit_and_immutable_digest():
    payload = _task()
    payload.update(agent_id="rusa_deploy_agent", stage="deploy")
    payload["input"] = {
        "release_manifest_ref": "artifact://release/manifest",
        "environment": "staging",
        "commit_sha": "a" * 40,
        "artifact_digest": "sha256:" + "b" * 64,
    }
    payload["constraints"]["allowed_tools"] = [
        "artifact.read", "release.read", "deployment.execute", "health.read", "runbook.read"
    ]
    validate_agent_task(payload)
    payload["input"].pop("commit_sha")
    with pytest.raises(ContractValidationError, match="commit_sha"):
        validate_agent_task(payload)

