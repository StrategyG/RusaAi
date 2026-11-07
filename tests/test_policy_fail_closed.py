"""Fail-closed behavior of AutonomyPolicy allowlists."""
from rusa_agents.policy import AutonomyPolicy, RiskLevel


def test_omitted_allowlist_uses_defaults():
    assert AutonomyPolicy().evaluate("repo_read").allowed is True


def test_explicit_empty_allowlist_denies_everything():
    decision = AutonomyPolicy(set()).evaluate("repo_read")
    assert decision.allowed is False
    assert "not allowlisted" in decision.reason


def test_explicit_allowlist_is_not_widened_to_defaults():
    policy = AutonomyPolicy({"audit_read"})
    assert policy.evaluate("audit_read").allowed is True
    assert policy.evaluate("workspace_write").allowed is False


def test_protected_actions_require_approval_even_when_allowlisted():
    decision = AutonomyPolicy({"merge"}).evaluate("merge")
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_denied_actions_are_prohibited_even_when_allowlisted():
    decision = AutonomyPolicy({"force_push_main"}).evaluate("force_push_main")
    assert decision.allowed is False
    assert decision.requires_approval is False
    assert "prohibited" in decision.reason


def test_high_risk_requires_approval_for_allowlisted_action():
    decision = AutonomyPolicy().evaluate("repo_read", RiskLevel.HIGH)
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_policy_instances_do_not_share_default_allowlist():
    first = AutonomyPolicy()
    first.allowed_actions.add("merge_everything")
    assert "merge_everything" not in AutonomyPolicy().allowed_actions
