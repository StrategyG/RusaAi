import pytest

from rusa_agents.contracts import (
    CodingTask,
    RepositoryTarget,
    TaskRecord,
    TaskState,
)
from rusa_agents.policy import AutonomyPolicy, RiskLevel


def make_task() -> CodingTask:
    return CodingTask(
        "Add coding agent",
        RepositoryTarget("StrategyG/RusaAi", "main", "feature/test"),
        ["Tests pass"],
    )


def test_task_requires_acceptance_criteria():
    with pytest.raises(ValueError):
        CodingTask("x", RepositoryTarget("owner/repo"), [])


def test_invalid_transition_rejected():
    record = TaskRecord(make_task())
    with pytest.raises(ValueError):
        record.transition(TaskState.SUCCEEDED)


def test_valid_state_path():
    record = TaskRecord(make_task())
    for state in (
        TaskState.OBSERVING,
        TaskState.PLANNING,
        TaskState.EXECUTING,
        TaskState.VERIFYING,
        TaskState.AWAITING_APPROVAL,
    ):
        record.transition(state)
    assert record.state == TaskState.AWAITING_APPROVAL


def test_explicit_empty_policy_allowlist_denies_every_unprotected_action():
    decision = AutonomyPolicy(set()).evaluate("repo_read")
    assert not decision.allowed
    assert not decision.requires_approval


def test_policy_requires_approval_for_protected_and_high_risk_actions():
    policy = AutonomyPolicy()
    protected = policy.evaluate("deploy")
    high_risk = policy.evaluate("repo_read", RiskLevel.HIGH)
    assert not protected.allowed and protected.requires_approval
    assert not high_risk.allowed and high_risk.requires_approval


def test_policy_fails_closed_on_invalid_risk_level_and_action():
    policy = AutonomyPolicy()
    assert not policy.evaluate("repo_read", "unknown").allowed
    assert not policy.evaluate("", RiskLevel.LOW).allowed



def test_policy_preserves_explicit_empty_allowlist():
    policy = AutonomyPolicy(set())
    decision = policy.evaluate("repo_read")
    assert not decision.allowed
    assert not decision.requires_approval


def test_policy_rejects_invalid_action_and_risk_values():
    policy = AutonomyPolicy({"repo_read"})
    assert not policy.evaluate("").allowed
    assert not policy.evaluate("repo_read", "untrusted-risk").allowed
    assert policy.evaluate("repo_read", "high").requires_approval
