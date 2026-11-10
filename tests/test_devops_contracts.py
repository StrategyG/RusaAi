from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from rusa_agents.devops_contracts import (
    AGENT_PROFILES,
    ContractValidationError,
    authorize_agent_action,
    get_agent_profile,
    validate_agent_result,
    validate_agent_task,
    validate_workflow_event,
    validate_workflow_handoff,
)


def make_task(**overrides):
    now = datetime.now(UTC)
    task = {
        "schema_version": 1,
        "task_id": str(uuid4()),
        "workflow_id": str(uuid4()),
        "tenant_id": "tenant-contract-test",
        "agent_id": "rusa_plan_agent",
        "stage": "plan",
        "objective": "Create a bounded implementation plan",
        "input": {"repository": "StrategyG/RusaAi", "description": "Contract dry-run only"},
        "constraints": {
            "allowed_tools": ["repository.read", "plan.write", "task.create"],
            "network_access": False,
            "write_scope": ["plans/"],
            "approval_required": False,
            "requested_actions": ["repository.read", "plan.create"],
        },
        "priority": "P2",
        "attempt": 1,
        "max_attempts": 3,
        "idempotency_key": f"workflow:{uuid4()}",
        "correlation_id": str(uuid4()),
        "deadline_at": (now + timedelta(minutes=2)).isoformat(),
        "created_at": now.isoformat(),
    }
    task.update(overrides)
    return task


def make_result(task):
    result_id = str(uuid4())
    digest = "a" * 64
    return {
        "schema_version": 1,
        "result_id": result_id,
        "task_id": task["task_id"],
        "workflow_id": task["workflow_id"],
        "tenant_id": task["tenant_id"],
        "agent_id": task["agent_id"],
        "stage": task["stage"],
        "attempt": task["attempt"],
        "status": "passed",
        "summary": "Dummy planner produced a plan artifact without invoking tools",
        "artifacts": [{"ref": f"artifact://plans/{result_id}", "digest": f"sha256:{digest}"}],
        "evidence": [{
            "kind": "plan",
            "ref": f"artifact://plans/{result_id}",
            "sha256": digest,
            "description": "Contract dry-run evidence",
        }],
        "findings": [],
        "risks": [],
        "unresolved_issues": [],
        "next_actions": ["Review plan before enabling a real worker"],
        "side_effect": "NONE",
        "started_at": task["created_at"],
        "completed_at": datetime.now(UTC).isoformat(),
        "audit": {"policy_version": "devops-policy-v1", "model_id": "dummy/no-model"},
    }


def make_event(task, result):
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    span = "00f067aa0ba902b7"
    artifact_ref = result["artifacts"][0]["ref"]
    return {
        "schema_version": 1,
        "type": "plan.completed",
        "message_id": str(uuid4()),
        "tenant_id": task["tenant_id"],
        "workflow_id": task["workflow_id"],
        "run_id": task["workflow_id"],
        "task_id": task["task_id"],
        "attempt": task["attempt"],
        "correlation_id": task["correlation_id"],
        "traceparent": f"00-{trace}-{span}-01",
        "producer": task["agent_id"],
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": {
            "stage": "plan",
            "status": "passed",
            "result_ref": artifact_ref,
            "evidence_refs": [result["evidence"][0]["ref"]],
        },
    }


def test_agent_task_result_and_event_contract_dry_run():
    task = make_task()
    validate_agent_task(task)

    # Dummy agent has no tool gateway and cannot perform repository or deployment actions.
    result = make_result(task)
    validate_agent_result(result, task)
    event = make_event(task, result)
    validate_workflow_handoff(task, result, event)

    assert event["type"] == "plan.completed"
    assert result["side_effect"] == "NONE"


def test_registry_covers_ten_general_agents_and_eight_lifecycle_agents():
    assert len(AGENT_PROFILES) == 20
    for agent_id in (
        "supreme_orchestrator", "system_architect", "planner", "task_decomposer",
        "software_engineer", "devops", "qa_testing", "security_policy",
        "memory_knowledge", "evaluation_reporting",
    ):
        assert get_agent_profile(agent_id).agent_id == agent_id
    for agent_id in (
        "rusa_plan_agent", "rusa_code_agent", "rusa_build_agent", "rusa_test_agent",
        "rusa_release_agent", "rusa_deploy_agent", "rusa_operate_agent", "rusa_monitor_agent",
    ):
        assert get_agent_profile(agent_id).stage is not None


@pytest.mark.parametrize("action", [
    "merge_protected_branch", "branch_protection.change", "deploy_staging",
    "deploy_production", "database_migration.risky", "resource.delete",
    "secret.change", "identity.change", "security_policy.change",
    "budget.override", "social.publish_private_asset",
])
def test_protected_actions_always_require_human_approval(action):
    decision = authorize_agent_action("rusa_deploy_agent", action)
    assert not decision.allowed
    assert decision.requires_human_approval


def test_agent_cannot_escalate_tools_write_scope_or_network_access():
    task = make_task()
    task["constraints"]["allowed_tools"].append("secret.read")
    with pytest.raises(ContractValidationError, match="tools outside"):
        validate_agent_task(task)

    task = make_task()
    task["constraints"]["write_scope"] = ["production/"]
    with pytest.raises(ContractValidationError, match="write scope"):
        validate_agent_task(task)

    task = make_task(
        agent_id="rusa_build_agent",
        stage="build",
        constraints={
            "allowed_tools": ["source.read", "container.build", "artifact.write"],
            "network_access": True,
            "write_scope": ["build-artifacts/"],
            "approval_required": False,
            "requested_actions": ["source.read", "container.build", "artifact.write"],
        },
    )
    with pytest.raises(ContractValidationError, match="network access"):
        validate_agent_task(task)


def test_task_deadline_attempt_and_stage_identity_are_enforced():
    task = make_task(attempt=4)
    with pytest.raises(ContractValidationError):
        validate_agent_task(task)

    task = make_task(agent_id="rusa_build_agent")
    with pytest.raises(ContractValidationError, match="cannot execute stage"):
        validate_agent_task(task)

    task = make_task()
    task["deadline_at"] = task["created_at"]
    with pytest.raises(ContractValidationError, match="deadline"):
        validate_agent_task(task)


def test_protected_task_action_must_be_marked_for_approval_but_is_not_authorized():
    task = make_task()
    task["constraints"]["requested_actions"] = ["deploy_production"]
    with pytest.raises(ContractValidationError, match="approval_required"):
        validate_agent_task(task)

    task["constraints"]["approval_required"] = True
    validate_agent_task(task)
    decision = authorize_agent_action("rusa_plan_agent", "deploy_production")
    assert not decision.allowed
    assert decision.requires_human_approval


def test_passed_result_requires_evidence_and_matches_task():
    task = make_task()
    result = make_result(task)
    result["evidence"] = []
    with pytest.raises(ContractValidationError):
        validate_agent_result(result, task)

    result = make_result(task)
    result["tenant_id"] = "other-tenant"
    with pytest.raises(ContractValidationError, match="does not match"):
        validate_agent_result(result, task)


def test_event_rejects_mismatched_run_alias_and_forged_evidence_reference():
    task = make_task()
    result = make_result(task)
    event = make_event(task, result)
    event["run_id"] = str(uuid4())
    with pytest.raises(ContractValidationError, match="same workflow"):
        validate_workflow_event(event)

    event = make_event(task, result)
    event["payload"]["evidence_refs"] = ["artifact://forged"]
    with pytest.raises(ContractValidationError, match="not bound"):
        validate_workflow_handoff(task, result, event)


def test_event_requires_trace_context_and_event_specific_payload():
    task = make_task()
    result = make_result(task)
    event = make_event(task, result)
    event["traceparent"] = "not-a-trace"
    with pytest.raises(ContractValidationError):
        validate_workflow_event(event)

    event = make_event(task, result)
    event["payload"].pop("result_ref")
    with pytest.raises(ContractValidationError):
        validate_workflow_event(event)


def test_release_and_deploy_events_bind_to_immutable_result_artifacts():
    task = make_task(
        agent_id="rusa_release_agent",
        stage="release",
        constraints={
            "allowed_tools": ["evidence.read", "manifest.write", "release_candidate.create"],
            "network_access": False,
            "write_scope": ["release-manifests/"],
            "approval_required": False,
            "requested_actions": ["evidence.read", "manifest.write", "release_candidate.create"],
        },
    )
    result = make_result(task)
    release_ref = result["artifacts"][0]["ref"]
    approval_ref = result["evidence"][0]["ref"]
    event = make_event(task, result)
    event["type"] = "release.approved"
    event["payload"] = {
        "release_ref": release_ref,
        "manifest_digest": result["artifacts"][0]["digest"],
        "approval_ref": approval_ref,
        "approver": "release-manager",
    }
    validate_workflow_handoff(task, result, event)

    deploy_task = make_task(
        agent_id="rusa_deploy_agent",
        stage="deploy",
        constraints={
            "allowed_tools": ["deployment.status.read", "health_check.execute", "rollback.plan"],
            "network_access": False,
            "write_scope": ["deployment-records/"],
            "approval_required": False,
            "requested_actions": ["deployment.status.read", "health_check.execute", "rollback.plan"],
        },
    )
    deploy_result = make_result(deploy_task)
    deploy_event = make_event(deploy_task, deploy_result)
    deploy_event["type"] = "deploy.completed"
    deploy_event["payload"] = {
        "environment": "staging",
        "deployment_ref": deploy_result["artifacts"][0]["ref"],
        "health_status": "healthy",
        "artifact_digest": deploy_result["artifacts"][0]["digest"],
    }
    validate_workflow_handoff(deploy_task, deploy_result, deploy_event)


def test_deployment_result_cannot_claim_pass_when_health_is_unhealthy():
    task = make_task(
        agent_id="rusa_deploy_agent",
        stage="deploy",
        constraints={
            "allowed_tools": ["deployment.status.read", "health_check.execute", "rollback.plan"],
            "network_access": False,
            "write_scope": ["deployment-records/"],
            "approval_required": False,
            "requested_actions": ["deployment.status.read", "health_check.execute", "rollback.plan"],
        },
    )
    result = make_result(task)
    event = make_event(task, result)
    event["type"] = "deploy.completed"
    event["payload"] = {
        "environment": "staging",
        "deployment_ref": result["artifacts"][0]["ref"],
        "health_status": "unhealthy",
        "artifact_digest": result["artifacts"][0]["digest"],
    }
    with pytest.raises(ContractValidationError, match="healthy"):
        validate_workflow_handoff(task, result, event)
