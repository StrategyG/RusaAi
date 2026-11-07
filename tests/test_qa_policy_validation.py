"""Regression tests for implemented QA and policy enforcement controls."""
import pytest

from rusa_agents.audit import AuditLog, record_task_policy
from rusa_agents.contracts import WorkerResult, WorkerRole
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.policy import AutonomyPolicy, RiskLevel
from rusa_agents.tool_gateway import ToolDenied, WorkspaceToolGateway


@pytest.mark.parametrize("action", sorted(AutonomyPolicy.PROTECTED_ACTIONS))
def test_protected_actions_always_require_approval(action):
    decision = AutonomyPolicy().evaluate(action)

    assert decision.allowed is False
    assert decision.requires_approval is True


@pytest.mark.parametrize("action", sorted(AutonomyPolicy.DENIED_ACTIONS))
def test_prohibited_actions_are_denied(action):
    decision = AutonomyPolicy().evaluate(action)

    assert decision.allowed is False
    assert decision.requires_approval is False


def test_explicit_empty_allowlist_denies_every_action():
    decision = AutonomyPolicy(allowed_actions=set()).evaluate("repo_read")

    assert decision.allowed is False
    assert "not allowlisted" in decision.reason


def test_custom_allowlist_is_not_mutable_through_caller_reference():
    configured = {"repo_read"}
    policy = AutonomyPolicy(allowed_actions=configured)
    configured.clear()

    assert policy.evaluate("repo_read").allowed is True
    assert policy.evaluate("workspace_write").allowed is False


def test_protected_action_cannot_be_enabled_by_custom_allowlist():
    decision = AutonomyPolicy(allowed_actions={"deploy"}).evaluate("deploy")

    assert decision.allowed is False
    assert decision.requires_approval is True


@pytest.mark.parametrize("risk", [RiskLevel.HIGH, RiskLevel.CRITICAL])
def test_high_risk_action_requires_approval_even_when_allowlisted(risk):
    decision = AutonomyPolicy(allowed_actions={"repo_read"}).evaluate("repo_read", risk)

    assert decision.allowed is False
    assert decision.requires_approval is True


def test_quality_evidence_requires_the_expected_validation_profile():
    result = WorkerResult(
        role=WorkerRole.TESTER,
        summary="test result",
        success=True,
        evidence={"check:pytest": {"profile": "bandit", "exit_code": 0}},
    )

    assert AICodeOrchestrator._check_passed(result, "check:pytest", "pytest") is False


@pytest.mark.parametrize("exit_code", [False, "0", 0.0, 1, None])
def test_quality_evidence_requires_an_integer_zero_exit_code(exit_code):
    result = WorkerResult(
        role=WorkerRole.TESTER,
        summary="test result",
        success=True,
        evidence={"check:pytest": {"profile": "pytest", "exit_code": exit_code}},
    )

    assert AICodeOrchestrator._check_passed(result, "check:pytest", "pytest") is False


def test_quality_evidence_accepts_matching_profile_and_integer_zero():
    result = WorkerResult(
        role=WorkerRole.TESTER,
        summary="test result",
        success=True,
        evidence={"check:pytest": {"profile": "pytest", "exit_code": 0}},
    )

    assert AICodeOrchestrator._check_passed(result, "check:pytest", "pytest") is True


@pytest.mark.parametrize("role", [WorkerRole.TESTER, WorkerRole.SECURITY])
def test_quality_workers_cannot_modify_workspace(tmp_path, role):
    gateway = WorkspaceToolGateway(tmp_path)

    with pytest.raises(ToolDenied):
        gateway.write_file("src/unauthorized.py", "VALUE = True", role)


def test_workspace_gateway_does_not_register_production_deploy_tool(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)

    with pytest.raises(ToolDenied, match="not registered"):
        gateway.execute({"name": "deploy", "arguments": {}}, WorkerRole.DEVELOPER)


def test_required_policy_audit_fails_closed_when_sink_is_missing():
    with pytest.raises(RuntimeError, match="not configured"):
        record_task_policy(
            "tenant-a",
            "task-a",
            "worker:test",
            "task.execute",
            True,
            required=True,
        )


def test_required_policy_audit_fails_closed_when_sink_fails():
    class FailingSink:
        def record(self, tenant_id, task_id, actor, action, decision):
            raise OSError("audit database unavailable")

    with pytest.raises(RuntimeError, match="store is unavailable"):
        record_task_policy(
            "tenant-a",
            "task-a",
            "worker:test",
            "task.execute",
            True,
            sink=FailingSink(),
            required=True,
        )


def test_hmac_audit_requires_a_strong_key(tmp_path):
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        AuditLog(str(tmp_path / "audit.jsonl"), hmac_key="too-short", require_hmac=True)


def test_hmac_audit_detects_record_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(str(path), hmac_key="k" * 40, require_hmac=True)
    audit.record(
        "policy.decision",
        "task-a",
        "policy-engine",
        {"decision": "allow", "action": "repo_read"},
        tenant_id="tenant-a",
    )

    assert audit.verify() is True
    path.write_text(path.read_text(encoding="utf-8").replace('"allow"', '"deny"'), encoding="utf-8")
    assert audit.verify() is False
