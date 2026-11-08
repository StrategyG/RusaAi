from __future__ import annotations  # noqa: I001

from copy import deepcopy
from uuid import uuid4

import pytest

from rusa_agents.devops.agent_registry import (
    AGENT_POLICIES,
    BUILD_AGENT,
    CODE_AGENT,
    DEPLOY_AGENT,
    MONITOR_AGENT,
    OPERATE_AGENT,
    PLAN_AGENT,
    READ_ONLY_TOOLS,
    RELEASE_AGENT,
    SECURITY_GUARD_AGENT,
    TEST_AGENT,
    AgentAuthorizationError,
    authorize_agent_task,
)


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _task(stage: str, *, agent_id: str | None = None) -> dict:
    agent_id = agent_id or {
        "plan": PLAN_AGENT,
        "code": CODE_AGENT,
        "build": BUILD_AGENT,
        "test": TEST_AGENT,
        "release": RELEASE_AGENT,
        "deploy": DEPLOY_AGENT,
        "operate": OPERATE_AGENT,
        "monitor": MONITOR_AGENT,
    }[stage]
    inputs = {
        "plan": {"user_request": "Prepare a bounded implementation plan."},
        "code": {
            "repository": "StrategyG/RusaAi",
            "plan_ref": "artifact://plans/plan-1",
            "acceptance_criteria": ["All required tests pass."],
        },
        "build": {
            "repository": "StrategyG/RusaAi",
            "commit_sha": "a" * 40,
            "build_config_ref": "artifact://build/config-1",
        },
        "test": {
            "commit_sha": "a" * 40,
            "artifact_digest": "sha256:" + "b" * 64,
            "test_plan_ref": "artifact://tests/plan-1",
            "acceptance_criteria_ref": "artifact://acceptance/criteria-1",
        },
        "release": {
            "commit_sha": "a" * 40,
            "artifact_digest": "sha256:" + "b" * 64,
            "test_evidence_ref": "evidence://test/report-1",
            "sbom_ref": "artifact://sbom/sbom-1",
        },
        "deploy": {
            "release_manifest_ref": "artifact://release/manifest-1",
            "environment": "staging",
            "commit_sha": "a" * 40,
            "artifact_digest": "sha256:" + "b" * 64,
        },
        "operate": {"service": "api", "runbook_ref": "runbook://api/restart"},
        "monitor": {"service": "api", "metrics_window": "last-15m"},
    }[stage]
    if agent_id == SECURITY_GUARD_AGENT:
        inputs["evidence_refs"] = ["evidence://test/report-1"]

    policy = AGENT_POLICIES[agent_id]
    default_scope = {
        "plan": ["plans/"],
        "code": ["src/"],
        "build": ["build/"],
        "test": ["test-reports/"],
        "release": ["release/"],
        "deploy": ["deployments/"],
        "operate": ["incidents/"],
        "monitor": [],
    }[stage]
    return {
        "schema_version": 1,
        "task_id": str(uuid4()),
        "workflow_id": str(uuid4()),
        "tenant_id": "tenant-test",
        "agent_id": agent_id,
        "stage": stage,
        "objective": f"Execute the {stage} specialist stage.",
        "input": inputs,
        "constraints": {
            "allowed_tools": sorted(policy.allowed_tools),
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": default_scope,
            "approval_required": False,
            "approval_scope": [],
            "budget": {
                "max_cost_usd": 2.0,
                "max_tool_calls": 20,
                "max_runtime_seconds": 300,
            },
        },
        "attempt": 1,
        "audit": {
            "created_at": "2026-11-08T00:00:00Z",
            "requested_by": "pytest",
            "policy_version": "policy-test-v1",
            "traceparent": TRACEPARENT,
        },
    }


@pytest.mark.parametrize(
    ("stage", "agent_id"),
    [
        ("plan", PLAN_AGENT),
        ("code", CODE_AGENT),
        ("build", BUILD_AGENT),
        ("test", TEST_AGENT),
        ("release", RELEASE_AGENT),
        ("deploy", DEPLOY_AGENT),
        ("operate", OPERATE_AGENT),
        ("monitor", MONITOR_AGENT),
    ],
)
def test_registered_specialists_accept_only_their_own_stage(stage, agent_id):
    task = _task(stage)
    policy = authorize_agent_task(task, authenticated_principal=agent_id)
    assert policy.agent_id == agent_id
    assert policy.stage == stage


def test_security_guard_is_cross_cutting_but_read_only():
    task = _task("test", agent_id=SECURITY_GUARD_AGENT)
    task["constraints"]["allowed_tools"] = sorted(READ_ONLY_TOOLS)
    task["constraints"]["write_scope"] = []

    policy = authorize_agent_task(
        task,
        authenticated_principal=SECURITY_GUARD_AGENT,
    )

    assert policy.stage is None
    assert policy.read_only is True


def test_task_body_cannot_impersonate_another_authenticated_agent():
    task = _task("code")
    with pytest.raises(AgentAuthorizationError, match="authenticated principal"):
        authorize_agent_task(task, authenticated_principal=PLAN_AGENT)


def test_code_agent_cannot_escalate_to_deployment_or_production_tools():
    task = _task("code")
    task["constraints"]["allowed_tools"].append("deployment.execute")
    with pytest.raises(AgentAuthorizationError, match="outside the agent allowlist"):
        authorize_agent_task(task, authenticated_principal=CODE_AGENT)


@pytest.mark.parametrize(
    "scope",
    [
        "migrations/010_new_policy.sql",
        "src/migrations/010_new_policy.sql",
        "src/infra/terraform/main.tf",
        ".github/workflows/ci.yml",
        "src/.env.production",
        "src/credentials/token.json",
        "src/private-key.pem",
        "pyproject.toml",
        ".gitignore",
        "Dockerfile",
        "requirements-dev.txt",
        ".devcontainer/devcontainer.json",
    ],
)
def test_code_agent_cannot_receive_protected_repository_write_scope(scope):
    task = _task("code")
    task["constraints"]["write_scope"] = [scope]
    with pytest.raises(AgentAuthorizationError, match="protected repository path"):
        authorize_agent_task(task, authenticated_principal=CODE_AGENT)


def test_stage_artifact_writes_are_limited_to_their_scoped_directories():
    task = _task("build")
    task["constraints"]["write_scope"] = ["src/"]
    with pytest.raises(AgentAuthorizationError, match="outside the build agent"):
        authorize_agent_task(task, authenticated_principal=BUILD_AGENT)


def test_monitor_cannot_be_given_write_tools_or_write_scope():
    task = _task("monitor")
    task["constraints"]["allowed_tools"].append("incident.write")
    with pytest.raises(AgentAuthorizationError, match="outside the agent allowlist"):
        authorize_agent_task(task, authenticated_principal=MONITOR_AGENT)

    task = _task("monitor")
    task["constraints"]["write_scope"] = ["reports/"]
    with pytest.raises(AgentAuthorizationError, match="read-only"):
        authorize_agent_task(task, authenticated_principal=MONITOR_AGENT)


def test_network_access_is_denied_until_a_stage_has_an_explicit_network_policy():
    task = _task("code")
    task["constraints"]["network_access"] = True
    task["constraints"]["allowed_network_destinations"] = ["github.com"]
    with pytest.raises(AgentAuthorizationError, match="network access"):
        authorize_agent_task(task, authenticated_principal=CODE_AGENT)


def test_production_deployment_requires_an_explicit_human_approval_reference():
    task = _task("deploy")
    task["input"]["environment"] = "production"
    with pytest.raises(AgentAuthorizationError, match="production deployment requires"):
        authorize_agent_task(task, authenticated_principal=DEPLOY_AGENT)

    approved = deepcopy(task)
    approved["constraints"]["approval_required"] = True
    approved["constraints"]["approval_scope"] = ["deploy"]
    approved["constraints"]["approval_ref"] = "approval://human/approved-123"
    policy = authorize_agent_task(
        approved,
        authenticated_principal=DEPLOY_AGENT,
    )
    assert policy.agent_id == DEPLOY_AGENT


def test_approval_required_must_name_an_approval_scope():
    task = _task("deploy")
    task["constraints"]["approval_required"] = True
    with pytest.raises(AgentAuthorizationError, match="explicit approval_scope"):
        authorize_agent_task(task, authenticated_principal=DEPLOY_AGENT)


def test_registry_is_complete_for_eight_stages_plus_cross_cutting_guard():
    expected = {
        PLAN_AGENT,
        CODE_AGENT,
        BUILD_AGENT,
        TEST_AGENT,
        RELEASE_AGENT,
        DEPLOY_AGENT,
        OPERATE_AGENT,
        MONITOR_AGENT,
        SECURITY_GUARD_AGENT,
    }
    assert set(AGENT_POLICIES) == expected
