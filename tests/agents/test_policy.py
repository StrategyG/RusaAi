from rusa_agents.policy import AutonomyPolicy, RiskLevel


def test_allowlisted_low_risk_action_is_autonomous():
    assert AutonomyPolicy().evaluate("workspace.write").allowed


def test_protected_action_requires_approval():
    policy = AutonomyPolicy()
    decision = policy.evaluate("deployment.production")
    assert not decision.allowed
    assert decision.approval_required
    assert policy.evaluate("deployment.production", approval_granted=True).allowed


def test_unknown_action_fails_closed():
    assert not AutonomyPolicy().evaluate("arbitrary.shell").allowed


def test_prohibited_action_cannot_be_approved():
    decision = AutonomyPolicy().evaluate("audit.disable", approval_granted=True)
    assert not decision.allowed
    assert not decision.approval_required


def test_high_risk_action_requires_approval():
    assert AutonomyPolicy().evaluate("agent.execute", RiskLevel.HIGH).approval_required
