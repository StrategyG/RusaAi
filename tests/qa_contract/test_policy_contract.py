"""Immutable QA contract tests for autonomy policy decisions."""
import pytest

from rusa_agents.policy import AutonomyPolicy, RiskLevel


@pytest.mark.parametrize("action", sorted(AutonomyPolicy.PROTECTED_ACTIONS))
def test_every_protected_action_requires_human_approval(action):
    decision = AutonomyPolicy().evaluate(action)

    assert decision.allowed is False
    assert decision.requires_approval is True


@pytest.mark.parametrize("action", sorted(AutonomyPolicy.DENIED_ACTIONS))
def test_every_prohibited_action_is_denied(action):
    decision = AutonomyPolicy().evaluate(action)

    assert decision.allowed is False
    assert decision.requires_approval is False


def test_empty_custom_allowlist_does_not_fall_back_to_defaults():
    policy = AutonomyPolicy(allowed_actions=set())

    assert policy.evaluate("repo_read").allowed is False


def test_protected_action_cannot_be_allowlisted():
    policy = AutonomyPolicy(allowed_actions={"deploy"})

    decision = policy.evaluate("deploy")
    assert decision.allowed is False
    assert decision.requires_approval is True


@pytest.mark.parametrize("risk", [RiskLevel.HIGH, RiskLevel.CRITICAL])
def test_high_risk_requires_approval_even_for_allowlisted_action(risk):
    policy = AutonomyPolicy(allowed_actions={"repo_read"})

    decision = policy.evaluate("repo_read", risk)
    assert decision.allowed is False
    assert decision.requires_approval is True
