from datetime import datetime, timedelta, timezone

import pytest

from rusa_agents.approvals import _binding_digest
from rusa_agents.capability_auth import (
    CapabilityDenied,
    TaskScopedGrant,
    ToolAuthorizationContext,
    authorize_tool_invocation,
)
from rusa_agents.contracts import CodingTask, RepositoryTarget, TaskRecord, WorkerRole
from rusa_agents.domain_orchestrator import (
    AICodeDomainOrchestratorAdapter,
    DomainAccessGrant,
    DomainOrchestratorError,
    authorize_domain_handoff,
)
from rusa_agents.platform_contracts import (
    AgentDomain,
    AgentHealthStatus,
    AgentManifestV1,
    AgentStatus,
    PolicyDecisionV1,
    RuntimeKind,
    WorkflowEventV2,
)
from rusa_agents.policy import RiskLevel
from rusa_agents.policy_engine import (
    DeterministicPolicyEngine,
    PolicyDecisionService,
    PolicyRequest,
)
from rusa_agents.registry import _manifest_hash, _tenant_allowed


NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def manifest(**overrides):
    values = {
        "agent_id": "rusa_test_agent",
        "domain": AgentDomain.SOFTWARE_ENGINEERING,
        "version": "1.0.0",
        "status": AgentStatus.ACTIVE,
        "capabilities": ["repo.read"],
        "input_schema_ref": "schema://rusa/repo/read/input.v1",
        "output_schema_ref": "schema://rusa/repo/read/output.v1",
        "runtime_binding": {
            "kind": RuntimeKind.PYTHON_CALLABLE,
            "target": "tests.fake_agents:ReadAgent",
        },
        "tool_allowlist": ["list_files"],
        "tenant_scope": {"mode": "tenant"},
        "resource_scope": {"include_patterns": ["repo://StrategyG/RusaAi/*"]},
        "policy_profile": "software-engineering-default",
        "evaluation_profile": "repo-read-v1",
        "health": {"status": AgentHealthStatus.HEALTHY, "checked_at": NOW},
        "owner": "platform-tests",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return AgentManifestV1.model_validate(values)


def policy_decision(**overrides):
    values = {
        "tenant_id": "tenant-a",
        "actor": "rusa_test_agent",
        "action": "tool.invoke:list_files",
        "resource_ref": "repo://StrategyG/RusaAi/src",
        "policy_id": "software-engineering-default",
        "policy_version": "1",
        "outcome": "allow",
        "reason_code": "policy_allowlist_match",
        "input_sha256": "sha256:" + "a" * 64,
        "evaluated_at": NOW,
        "task_id": "task-1",
    }
    values.update(overrides)
    return PolicyDecisionV1.model_validate(values)


def authorization_context(**overrides):
    grant = TaskScopedGrant(
        tenant_id="tenant-a",
        task_id="task-1",
        agent_id="rusa_test_agent",
        agent_version="1.0.0",
        capability="repo.read",
        allowed_tools=frozenset({"list_files"}),
        expires_at=NOW + timedelta(minutes=2),
        grant_id="grant-1",
        resource_patterns=("repo://StrategyG/RusaAi/*",),
    )
    values = {
        "tenant_id": "tenant-a",
        "registry_tenant_id": "tenant-a",
        "task_id": "task-1",
        "agent_id": "rusa_test_agent",
        "agent_version": "1.0.0",
        "capability": "repo.read",
        "tool_name": "list_files",
        "resource_ref": "repo://StrategyG/RusaAi/src",
        "worker_role": WorkerRole.ANALYST,
        "task_deadline_at": NOW + timedelta(minutes=5),
        "grant": grant,
        "policy_decision": policy_decision(),
    }
    values.update(overrides)
    return ToolAuthorizationContext(**values)


def test_policy_engine_allows_only_explicit_low_risk_allowlist_actions():
    engine = DeterministicPolicyEngine(
        policy_id="software-engineering-default",
        policy_version="1",
        allowed_actions={"repo.read", "tool.invoke:list_files"},
    )
    request = PolicyRequest(
        tenant_id="tenant-a",
        authenticated_tenant_id="tenant-a",
        actor="rusa_test_agent",
        action="repo.read",
        resource_ref="repo://StrategyG/RusaAi",
        policy_id="software-engineering-default",
        policy_version="1",
        risk=RiskLevel.LOW,
    )
    decision = engine.evaluate(request)
    assert decision.outcome == "allow"
    assert decision.reason_code == "policy_allowlist_match"
    assert decision.input_sha256.startswith("sha256:") and len(decision.input_sha256) == 71


@pytest.mark.parametrize(
    ("changes", "outcome", "reason"),
    [
        ({"authenticated_tenant_id": "tenant-b"}, "deny", "tenant_mismatch"),
        ({"action": "disable_audit"}, "deny", "action_prohibited"),
        ({"action": "unknown.action"}, "deny", "action_not_allowlisted"),
        ({"action": "unknown.high_risk", "risk": RiskLevel.HIGH}, "deny", "action_not_allowlisted"),
        ({"risk": RiskLevel.HIGH}, "require_approval", "human_approval_required"),
        ({"requires_approval": True}, "require_approval", "human_approval_required"),
    ],
)
def test_policy_engine_denies_or_requires_approval_deterministically(changes, outcome, reason):
    engine = DeterministicPolicyEngine(
        policy_id="software-engineering-default",
        policy_version="1",
        allowed_actions={"repo.read"},
    )
    values = {
        "tenant_id": "tenant-a",
        "authenticated_tenant_id": "tenant-a",
        "actor": "rusa_test_agent",
        "action": "repo.read",
        "resource_ref": "repo://StrategyG/RusaAi",
        "policy_id": "software-engineering-default",
        "policy_version": "1",
        "risk": RiskLevel.LOW,
    }
    values.update(changes)
    decision = engine.evaluate(PolicyRequest(**values))
    assert (decision.outcome, decision.reason_code) == (outcome, reason)


def test_policy_engine_only_releases_protected_action_after_exact_approval_consumption():
    engine = DeterministicPolicyEngine(
        policy_id="release-policy",
        policy_version="4",
        allowed_actions={"release.promote"},
    )
    base = {
        "tenant_id": "tenant-a",
        "authenticated_tenant_id": "tenant-a",
        "actor": "release-agent",
        "action": "release.promote",
        "resource_ref": "artifact://release/candidate",
        "policy_id": "release-policy",
        "policy_version": "4",
        "risk": RiskLevel.HIGH,
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "task_id": "task-1",
    }
    pending = engine.evaluate(PolicyRequest(**base))
    assert pending.outcome == "require_approval"
    assert pending.approval_id is None

    approved = engine.evaluate(
        PolicyRequest(**base, approval_consumed=True, approval_id="approval-1")
    )
    assert approved.outcome == "allow"
    assert approved.reason_code == "approved_action_authorized"
    assert approved.approval_id == "approval-1"

    with pytest.raises(ValueError, match="only after trusted one-time consumption"):
        engine.evaluate(PolicyRequest(**base, approval_id="approval-1"))
    with pytest.raises(ValueError, match="must include its approval ID"):
        engine.evaluate(PolicyRequest(**base, approval_consumed=True))


def test_policy_denial_is_stable_for_same_input_and_changes_when_input_changes():
    engine = DeterministicPolicyEngine(
        policy_id="policy",
        policy_version="7",
        allowed_actions={"repo.read"},
    )
    base = PolicyRequest(
        tenant_id="tenant-a",
        authenticated_tenant_id="tenant-a",
        actor="agent-a",
        action="repo.delete",
        resource_ref="repo://owner/repo",
        policy_id="policy",
        policy_version="7",
        context={"branch": "main"},
    )
    first = engine.evaluate(base)
    second = engine.evaluate(base)
    changed = engine.evaluate(
        PolicyRequest(
            tenant_id="tenant-a",
            authenticated_tenant_id="tenant-a",
            actor="agent-a",
            action="repo.delete",
            resource_ref="repo://owner/repo",
            policy_id="policy",
            policy_version="7",
            context={"branch": "release"},
        )
    )
    assert first.outcome == second.outcome == "deny"
    assert first.input_sha256 == second.input_sha256
    assert changed.input_sha256 != first.input_sha256


def test_policy_context_rejects_credentials_and_wrong_policy_version():
    engine = DeterministicPolicyEngine(
        policy_id="policy",
        policy_version="1",
        allowed_actions={"repo.read"},
    )
    base = {
        "tenant_id": "tenant-a",
        "authenticated_tenant_id": "tenant-a",
        "actor": "agent-a",
        "action": "repo.read",
        "resource_ref": "repo://owner/repo",
        "policy_id": "policy",
        "policy_version": "1",
    }
    with pytest.raises(ValueError, match="credential-shaped"):
        engine.evaluate(PolicyRequest(**base, context={"api_key": "never-persist"}))
    with pytest.raises(ValueError, match="loaded deterministic policy"):
        engine.evaluate(PolicyRequest(**{**base, "policy_version": "2"}))


def test_registry_hash_ignores_mutable_health_but_covers_immutable_capabilities():
    original = manifest(status=AgentStatus.DRAFT, health={"status": "unknown"})
    changed_health = manifest(
        status=AgentStatus.VALIDATED,
        health={"status": "healthy", "checked_at": NOW + timedelta(minutes=1)},
        updated_at=NOW + timedelta(minutes=1),
    )
    changed_tool = manifest(
        status=AgentStatus.DRAFT,
        health={"status": "unknown"},
        tool_allowlist=["list_files", "read_file"],
    )
    assert _manifest_hash(original) == _manifest_hash(changed_health)
    assert _manifest_hash(original) != _manifest_hash(changed_tool)


def test_registry_tenant_scope_distinguishes_platform_and_allowlist():
    system = manifest(tenant_scope={"mode": "system"})
    allowlist = manifest(tenant_scope={"mode": "allowlist", "tenant_ids": ["tenant-a", "tenant-c"]})
    tenant = manifest()
    assert _tenant_allowed(system, "tenant-a", "platform")
    assert _tenant_allowed(allowlist, "tenant-c", "platform")
    assert not _tenant_allowed(allowlist, "tenant-b", "platform")
    assert _tenant_allowed(tenant, "tenant-a", "tenant-a")
    assert not _tenant_allowed(tenant, "tenant-b", "tenant-a")


def test_capability_authorization_requires_manifest_grant_role_resource_and_policy():
    authorize_tool_invocation(manifest(), authorization_context(), now=NOW)

    with pytest.raises(CapabilityDenied, match="manifest, task grant"):
        authorize_tool_invocation(
            manifest(),
            authorization_context(
                grant=TaskScopedGrant(
                    tenant_id="tenant-a",
                    task_id="task-1",
                    agent_id="rusa_test_agent",
                    agent_version="1.0.0",
                    capability="repo.read",
                    allowed_tools=frozenset({"read_file"}),
                    expires_at=NOW + timedelta(minutes=2),
                    grant_id="grant-2",
                )
            ),
            now=NOW,
        )


def test_capability_authorization_requires_exact_task_and_manifest_policy_binding():
    with pytest.raises(CapabilityDenied, match="deterministic policy"):
        authorize_tool_invocation(
            manifest(),
            authorization_context(policy_decision=policy_decision(task_id=None)),
            now=NOW,
        )
    with pytest.raises(CapabilityDenied, match="deterministic policy"):
        authorize_tool_invocation(
            manifest(),
            authorization_context(policy_decision=policy_decision(policy_id="other-policy")),
            now=NOW,
        )


def test_capability_authorization_rejects_unbounded_manifest_or_task_resource_scope():
    with pytest.raises(CapabilityDenied, match="no explicit include patterns"):
        authorize_tool_invocation(
            manifest(resource_scope={}),
            authorization_context(),
            now=NOW,
        )
    with pytest.raises(CapabilityDenied, match="no explicit resource patterns"):
        authorize_tool_invocation(
            manifest(),
            authorization_context(
                grant=TaskScopedGrant(
                    tenant_id="tenant-a",
                    task_id="task-1",
                    agent_id="rusa_test_agent",
                    agent_version="1.0.0",
                    capability="repo.read",
                    allowed_tools=frozenset({"list_files"}),
                    expires_at=NOW + timedelta(minutes=2),
                    grant_id="grant-unscoped",
                )
            ),
            now=NOW,
        )


def test_capability_authorization_rejects_tenant_policy_and_deadline_mismatch():
    with pytest.raises(CapabilityDenied, match="tenant scope"):
        authorize_tool_invocation(
            manifest(),
            authorization_context(tenant_id="tenant-b", registry_tenant_id="tenant-a"),
            now=NOW,
        )
    with pytest.raises(CapabilityDenied, match="deterministic policy"):
        authorize_tool_invocation(
            manifest(),
            authorization_context(policy_decision=policy_decision(outcome="require_approval")),
            now=NOW,
        )
    with pytest.raises(CapabilityDenied, match="deadline has expired"):
        authorize_tool_invocation(
            manifest(),
            authorization_context(task_deadline_at=NOW - timedelta(seconds=1)),
            now=NOW,
        )


def test_approval_binding_changes_for_any_action_or_artifact_change():
    common = {
        "tenant_id": "tenant-a",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "action": "release.promote",
        "resource_ref": "artifact://release/candidate",
        "artifact_digest": "a" * 64,
        "policy_id": "release-policy",
        "policy_version": "4",
        "expires_at": NOW + timedelta(hours=1),
    }
    binding = _binding_digest(**common)
    assert binding == _binding_digest(**common)
    assert binding != _binding_digest(**{**common, "artifact_digest": "b" * 64})
    assert binding != _binding_digest(**{**common, "action": "release.delete"})


def test_domain_orchestrator_adapter_preserves_legacy_aicode_contract():
    task = CodingTask(
        "Add a test",
        RepositoryTarget("StrategyG/RusaAi"),
        ["Tests pass"],
        metadata={"tenant_id": "tenant-a"},
    )

    class FakeAICode:
        async def run(self, incoming):
            assert incoming.task_id == task.task_id
            return TaskRecord(incoming)

    import asyncio

    adapter = AICodeDomainOrchestratorAdapter(FakeAICode())
    plan = asyncio.run(adapter.plan(task))
    record = asyncio.run(adapter.dispatch(task))
    report = asyncio.run(adapter.final_report(record))
    assert plan.execution_mode == "legacy_aicode_passthrough"
    assert report["tenant_id"] == "tenant-a"
    assert report["status"] == "received"
    with pytest.raises(DomainOrchestratorError, match="progress reader"):
        asyncio.run(adapter.progress("tenant-a", task.task_id))


def test_domain_adapter_requires_authenticated_tenant_metadata():
    task = CodingTask(
        "Add a test",
        RepositoryTarget("StrategyG/RusaAi"),
        ["Tests pass"],
    )

    class FakeAICode:
        async def run(self, incoming):
            return TaskRecord(incoming)

    import asyncio

    adapter = AICodeDomainOrchestratorAdapter(FakeAICode())
    with pytest.raises(DomainOrchestratorError, match="tenant_id is required"):
        asyncio.run(adapter.plan(task))


def test_cross_domain_handoff_requires_explicit_unexpired_tenant_grant():
    with pytest.raises(DomainOrchestratorError, match="explicit platform grant"):
        authorize_domain_handoff(
            tenant_id="tenant-a",
            source_domain="software_engineering",
            target_domain="business",
            grant=None,
            now=NOW,
        )
    grant = DomainAccessGrant(
        tenant_id="tenant-a",
        source_domain="software_engineering",
        target_domain="business",
        actor="supreme-orchestrator",
        grant_id="handoff-1",
        expires_at=NOW + timedelta(minutes=1),
    )
    authorize_domain_handoff(
        tenant_id="tenant-a",
        source_domain="software_engineering",
        target_domain="business",
        grant=grant,
        now=NOW,
    )
    with pytest.raises(DomainOrchestratorError, match="invalid"):
        authorize_domain_handoff(
            tenant_id="tenant-b",
            source_domain="software_engineering",
            target_domain="business",
            grant=grant,
            now=NOW,
        )


def test_workflow_event_payload_is_versioned_and_tenant_bound():
    event = WorkflowEventV2(
        tenant_id="tenant-a",
        workflow_id="workflow-1",
        run_id="run-1",
        correlation_id="workflow-1",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        event_type="workflow.created",
        producer_id="rusa.workflow_store",
        producer_version="2.0.0",
        payload={"task_count": 3, "dependency_count": 2},
        occurred_at=NOW,
    )
    assert event.schema_version == "2.0"
    assert event.tenant_id == "tenant-a"
