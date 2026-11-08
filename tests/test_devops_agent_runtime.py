from __future__ import annotations

from dataclasses import replace

import pytest

from rusa_agents.devops.agent_runtime import (
    AgentApprovalRequiredError,
    AgentAuthorizationError,
    AgentBudgetExceededError,
    AgentCapabilityDeniedError,
    AgentHandlerNotRegisteredError,
    DevOpsAgentRegistry,
    DevOpsAgentRuntime,
    ExecutionPolicyGrant,
    ScopedToolGateway,
    ToolInvocationDeniedError,
)


def _task(
    stage: str = "plan",
    agent_id: str = "rusa_plan_agent",
    *,
    tools: list[str] | None = None,
    write_scope: list[str] | None = None,
    network_access: bool = False,
    destinations: list[str] | None = None,
    environment: str = "staging",
    approval_required: bool = False,
    approval_scope: list[str] | None = None,
    approval_ref: str | None = None,
) -> dict:
    inputs = {
        "plan": {"user_request": "Prepare a bounded delivery plan."},
        "code": {
            "repository": "StrategyG/RusaAi",
            "plan_ref": "artifact://plans/current",
            "acceptance_criteria": ["The change is covered by tests."],
        },
        "build": {
            "repository": "StrategyG/RusaAi",
            "commit_sha": "a" * 40,
            "build_config_ref": "artifact://build/config",
        },
        "test": {
            "commit_sha": "a" * 40,
            "artifact_digest": "sha256:" + "b" * 64,
            "test_plan_ref": "artifact://test/plan",
            "acceptance_criteria_ref": "artifact://test/criteria",
        },
        "release": {
            "commit_sha": "a" * 40,
            "artifact_digest": "sha256:" + "b" * 64,
            "test_evidence_ref": "artifact://test/evidence",
            "sbom_ref": "artifact://sbom/current",
        },
        "deploy": {
            "release_manifest_ref": "artifact://release/manifest",
            "environment": environment,
            "commit_sha": "a" * 40,
            "artifact_digest": "sha256:" + "b" * 64,
        },
        "operate": {
            "service": "rusa-api",
            "runbook_ref": "artifact://runbooks/restart",
        },
        "monitor": {
            "service": "rusa-api",
            "metrics_window": "last-15m",
        },
    }
    return {
        "schema_version": 1,
        "task_id": "task-agent-registry-1",
        "workflow_id": "run-agent-registry-1",
        "tenant_id": "tenant-a",
        "agent_id": agent_id,
        "stage": stage,
        "objective": "Exercise DevOps specialist authorization.",
        "input": inputs[stage],
        "constraints": {
            "allowed_tools": tools if tools is not None else ["repository.read"],
            "network_access": network_access,
            "allowed_network_destinations": destinations or [],
            "write_scope": write_scope or [],
            "approval_required": approval_required,
            "approval_scope": approval_scope or [],
            "budget": {
                "max_cost_usd": 2.0,
                "max_tool_calls": 20,
                "max_runtime_seconds": 300,
            },
            **({"approval_ref": approval_ref} if approval_ref else {}),
        },
        "attempt": 1,
        "audit": {
            "created_at": "2026-11-08T00:00:00Z",
            "requested_by": "pytest",
            "policy_version": "policy-1",
        },
    }


def _grant(task: dict, **overrides) -> ExecutionPolicyGrant:
    constraints = task["constraints"]
    budget = constraints["budget"]
    values = {
        "tenant_id": task["tenant_id"],
        "principal": task["agent_id"],
        "policy_version": task["audit"]["policy_version"],
        "allowed_tools": frozenset(constraints["allowed_tools"]),
        "write_scope": tuple(constraints["write_scope"]),
        "network_access": constraints["network_access"],
        "allowed_network_destinations": frozenset(
            constraints.get("allowed_network_destinations", [])
        ),
        "approved_scopes": frozenset(constraints.get("approval_scope", [])),
        "approved_refs": frozenset(
            [constraints["approval_ref"]] if "approval_ref" in constraints else []
        ),
        "max_cost_usd": budget["max_cost_usd"],
        "max_tool_calls": budget["max_tool_calls"],
        "max_runtime_seconds": budget["max_runtime_seconds"],
    }
    values.update(overrides)
    return ExecutionPolicyGrant(**values)


def test_registry_contains_eight_specialists_and_cross_cutting_security_guard():
    registry = DevOpsAgentRegistry()
    assert {profile.stage for profile in registry.profiles if profile.stage} == {
        "plan", "code", "build", "test", "release", "deploy", "operate", "monitor"
    }
    guard = registry.profile_for("rusa_security_guard_agent")
    assert guard.stage is None
    assert guard.read_only is True


def test_registry_overrides_can_only_reduce_registered_authority():
    registry = DevOpsAgentRegistry()
    profiles = list(registry.profiles)
    index = next(i for i, profile in enumerate(profiles) if profile.agent_id == "rusa_plan_agent")
    profiles[index] = replace(
        profiles[index],
        allowed_tools=profiles[index].allowed_tools | {"deployment.execute"},
    )
    with pytest.raises(ValueError, match="cannot expand"):
        DevOpsAgentRegistry(tuple(profiles))


def test_authorization_binds_tenant_principal_stage_and_policy_version():
    task = _task()
    registry = DevOpsAgentRegistry()
    with pytest.raises(AgentAuthorizationError, match="tenant"):
        registry.authorize(
            task,
            authenticated_tenant_id="tenant-b",
            authenticated_principal="rusa_plan_agent",
            policy_grant=_grant(task),
        )
    with pytest.raises(AgentAuthorizationError, match="assigned"):
        registry.authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_code_agent",
            policy_grant=_grant(task, principal="rusa_code_agent"),
        )
    with pytest.raises(AgentAuthorizationError, match="policy version"):
        registry.authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_plan_agent",
            policy_grant=_grant(task, policy_version="stale-policy"),
        )


def test_code_agent_cannot_receive_capability_outside_its_registered_profile():
    task = _task(
        "code",
        "rusa_code_agent",
        tools=["repository.read", "task.create"],
    )
    with pytest.raises(AgentCapabilityDeniedError, match="agent registry"):
        DevOpsAgentRegistry().authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_code_agent",
            policy_grant=_grant(task),
        )


def test_policy_engine_grant_must_cover_every_requested_capability_and_scope():
    task = _task(
        tools=["repository.read", "plan.write"],
        write_scope=["plans/"],
    )
    registry = DevOpsAgentRegistry()
    with pytest.raises(AgentCapabilityDeniedError, match="Policy Engine grant"):
        registry.authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_plan_agent",
            policy_grant=_grant(task, allowed_tools=frozenset({"repository.read", "issue.read"})),
        )
    with pytest.raises(AgentCapabilityDeniedError, match="write scope"):
        registry.authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_plan_agent",
            policy_grant=_grant(task, write_scope=("docs/",)),
        )


def test_monitor_and_security_guard_are_read_only_even_if_task_requests_write():
    for agent_id, stage, inputs in (
        ("rusa_monitor_agent", "monitor", {"service": "rusa-api", "metrics_window": "last-15m"}),
        (
            "rusa_security_guard_agent",
            "deploy",
            {
                "release_manifest_ref": "artifact://release/manifest",
                "environment": "staging",
                "commit_sha": "a" * 40,
                "artifact_digest": "sha256:" + "b" * 64,
            },
        ),
    ):
        task = _task(agent_id=agent_id, stage=stage, tools=["incident.write"])
        task["input"] = inputs
        with pytest.raises(AgentCapabilityDeniedError):
            DevOpsAgentRegistry().authorize(
                task,
                authenticated_tenant_id="tenant-a",
                authenticated_principal=agent_id,
                policy_grant=_grant(task),
            )


def test_plan_agent_cannot_receive_network_access_without_registry_eligibility():
    task = _task(
        tools=["repository.read"],
        network_access=True,
        destinations=["metrics.internal"],
    )
    with pytest.raises(AgentCapabilityDeniedError, match="not eligible"):
        DevOpsAgentRegistry().authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_plan_agent",
            policy_grant=_grant(task, network_access=True),
        )


def test_network_access_requires_agent_eligibility_and_exact_policy_allowlist():
    task = _task(
        "code",
        "rusa_code_agent",
        tools=["repository.read"],
        network_access=True,
        destinations=["https://pypi.org/"],
    )
    registry = DevOpsAgentRegistry()
    grant = _grant(
        task,
        network_access=True,
        allowed_network_destinations=frozenset({"pypi.org"}),
    )
    execution = registry.authorize(
        task,
        authenticated_tenant_id="tenant-a",
        authenticated_principal="rusa_code_agent",
        policy_grant=grant,
    )
    assert execution.allowed_network_destinations == frozenset({"pypi.org"})

    denied = _grant(
        task,
        network_access=True,
        allowed_network_destinations=frozenset({"github.com"}),
    )
    with pytest.raises(AgentCapabilityDeniedError, match="network destinations"):
        registry.authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_code_agent",
            policy_grant=denied,
        )


def test_production_deploy_requires_a_policy_verified_approval_reference():
    task = _task(
        "deploy",
        "rusa_deploy_agent",
        tools=["artifact.read", "release.read", "deployment.execute"],
        environment="production",
        approval_required=True,
        approval_scope=["deploy"],
        approval_ref="approval://change/123",
    )
    registry = DevOpsAgentRegistry()
    grant = _grant(task, approved_refs=frozenset())
    with pytest.raises(AgentApprovalRequiredError, match="approval reference"):
        registry.authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_deploy_agent",
            policy_grant=grant,
        )
    approved = registry.authorize(
        task,
        authenticated_tenant_id="tenant-a",
        authenticated_principal="rusa_deploy_agent",
        policy_grant=_grant(task),
    )
    assert approved.approval_ref == "approval://change/123"
    assert approved.approval_scopes == frozenset({"deploy"})


def test_task_budget_cannot_exceed_policy_engine_grant():
    task = _task()
    with pytest.raises(AgentBudgetExceededError):
        DevOpsAgentRegistry().authorize(
            task,
            authenticated_tenant_id="tenant-a",
            authenticated_principal="rusa_plan_agent",
            policy_grant=_grant(task, max_tool_calls=task["constraints"]["budget"]["max_tool_calls"] - 1),
        )


class _FakeToolInvoker:
    def __init__(self):
        self.calls = []

    async def invoke(self, tool_id, arguments):
        self.calls.append((tool_id, arguments))
        return {"ok": True}


@pytest.mark.asyncio
async def test_scoped_gateway_enforces_tool_path_network_and_call_budget():
    task = _task(
        tools=["plan.write", "repository.read"],
        write_scope=["plans/"],
    )
    grant = DevOpsAgentRegistry().authorize(
        task,
        authenticated_tenant_id="tenant-a",
        authenticated_principal="rusa_plan_agent",
        policy_grant=_grant(task, max_tool_calls=1),
    )
    delegate = _FakeToolInvoker()
    gateway = ScopedToolGateway(delegate, grant)

    assert await gateway.invoke("plan.write", {"path": "plans/approved.md"}) == {"ok": True}
    assert len(delegate.calls) == 1
    with pytest.raises(ToolInvocationDeniedError, match="outside the task execution grant"):
        await gateway.invoke("deployment.execute", {"environment": "staging"})
    with pytest.raises(AgentBudgetExceededError):
        await gateway.invoke("repository.read", {})


@pytest.mark.asyncio
async def test_scoped_gateway_rejects_path_traversal_and_unapproved_network_destination():
    task = _task(
        "code",
        "rusa_code_agent",
        tools=["repository.read", "artifact.write"],
        write_scope=["artifacts/"],
        network_access=True,
        destinations=["pypi.org"],
    )
    grant = DevOpsAgentRegistry().authorize(
        task,
        authenticated_tenant_id="tenant-a",
        authenticated_principal="rusa_code_agent",
        policy_grant=_grant(
            task,
            network_access=True,
            allowed_network_destinations=frozenset({"pypi.org"}),
        ),
    )
    gateway = ScopedToolGateway(_FakeToolInvoker(), grant)
    with pytest.raises(ValueError, match="unsafe path"):
        await gateway.invoke("artifact.write", {"path": "artifacts/../../secrets"})
    with pytest.raises(ToolInvocationDeniedError, match="network destination"):
        await gateway.invoke("repository.read", {"url": "https://evil.example/"})
    assert await gateway.invoke("repository.read", {"url": "https://pypi.org/"}) == {"ok": True}


class _FailingPlanHandler:
    async def execute(self, context):
        task = context.task
        return {
            "schema_version": 1,
            "task_id": task["task_id"],
            "workflow_id": task["workflow_id"],
            "tenant_id": task["tenant_id"],
            "agent_id": task["agent_id"],
            "stage": task["stage"],
            "attempt": task["attempt"],
            "status": "failed",
            "output": {
                "summary": "The planning input is incomplete.",
                "artifact_refs": [],
                "evidence_refs": [],
                "findings": [
                    {"severity": "medium", "code": "plan_incomplete", "message": "Missing acceptance detail."}
                ],
            },
            "audit": {
                "started_at": "2026-11-08T00:00:00Z",
                "completed_at": "2026-11-08T00:00:01Z",
                "policy_version": context.grant.policy_version,
            },
        }


@pytest.mark.asyncio
async def test_runtime_validates_handler_result_against_the_originating_task():
    task = _task()
    registry = DevOpsAgentRegistry()
    registry.register("rusa_plan_agent", _FailingPlanHandler())
    runtime = DevOpsAgentRuntime(registry)
    result = await runtime.execute(
        task,
        authenticated_tenant_id="tenant-a",
        authenticated_principal="rusa_plan_agent",
        policy_grant=_grant(task),
        tool_delegate=_FakeToolInvoker(),
    )
    assert result["status"] == "failed"
    assert result["task_id"] == task["task_id"]


def test_runtime_fails_closed_when_a_specialist_handler_is_not_registered():
    task = _task()
    with pytest.raises(AgentHandlerNotRegisteredError):
        DevOpsAgentRuntime(DevOpsAgentRegistry()).registry.handler_for(task["agent_id"])
