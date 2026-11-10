from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rusa_agents.devops.closed_loop import (
    ExecutionBudgetExceeded,
    KillSwitchActiveError,
    SLOAlert,
    WorkflowExecutionGuard,
    alert_fingerprint,
)


class FakeWorkflowStore:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def consume_budget(self, tenant_id, run_id, metric, amount):
        self.calls.append((tenant_id, run_id, metric, amount))
        if self.fail:
            raise RuntimeError("budget store unavailable")
        return {"consumed": float(amount), "limit": 100.0}


class FakeKillSwitchStore:
    def __init__(self, blocked_scope: str | None = None):
        self.blocked_scope = blocked_scope
        self.calls = []

    def assert_action_enabled(self, tenant_id, action):
        self.calls.append((tenant_id, action))
        if self.blocked_scope:
            raise KillSwitchActiveError(self.blocked_scope, "maintenance_window")


def _alert(**overrides):
    values = {
        "tenant_id": "tenant-a",
        "message_id": "monitor:message-1",
        "service_id": "rusa-api",
        "environment": "staging",
        "rule_id": "availability",
        "severity": "critical",
        "summary": "Availability is below the host threshold.",
        "evidence_ref": "evidence://sha256/" + "a" * 64,
        "observed_at": datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc),
        "correlation_id": "run-123",
        "workflow_id": "run-123",
        "metric_name": "availability",
        "operator": "gte",
        "threshold": 0.999,
        "observed_value": 0.98,
    }
    values.update(overrides)
    return SLOAlert(**values)


def test_alert_fingerprint_is_stable_across_windows_and_observations():
    first = _alert(observed_at=datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc))
    later = _alert(observed_at=datetime(2026, 11, 11, 1, 15, tzinfo=timezone.utc))
    other_rule = _alert(rule_id="latency")

    assert first.fingerprint == later.fingerprint
    assert first.fingerprint != other_rule.fingerprint
    assert len(first.fingerprint) == 64


def test_slo_alert_requires_immutable_evidence_and_finite_measurements():
    with pytest.raises(ValueError, match="content-addressed"):
        _alert(evidence_ref="https://example.invalid/metrics")
    with pytest.raises(ValueError, match="finite"):
        _alert(observed_value=float("nan"))


def test_execution_guard_checks_kill_switch_before_consuming_tool_budget():
    workflow = FakeWorkflowStore()
    switches = FakeKillSwitchStore()
    guard = WorkflowExecutionGuard(workflow_store=workflow, closed_loop_store=switches)

    result = guard.authorize_tool_call(tenant_id="tenant-a", run_id="run-123")

    assert result["consumed"] == 1.0
    assert switches.calls == [("tenant-a", "tool.invoke")]
    assert workflow.calls == [("tenant-a", "run-123", "tool_calls", 1)]


def test_execution_guard_blocks_before_budget_when_kill_switch_is_active():
    workflow = FakeWorkflowStore()
    switches = FakeKillSwitchStore(blocked_scope="deploy")
    guard = WorkflowExecutionGuard(workflow_store=workflow, closed_loop_store=switches)

    with pytest.raises(KillSwitchActiveError):
        guard.authorize_action(
            tenant_id="tenant-a", run_id="run-123", action="deploy",
            budget_metric="tool_calls", amount=1,
        )

    assert workflow.calls == []


def test_execution_guard_fails_closed_when_budget_cannot_be_confirmed():
    workflow = FakeWorkflowStore(fail=True)
    guard = WorkflowExecutionGuard(
        workflow_store=workflow,
        closed_loop_store=FakeKillSwitchStore(),
    )

    with pytest.raises(ExecutionBudgetExceeded):
        guard.authorize_tool_call(tenant_id="tenant-a", run_id="run-123")


def test_model_guard_reserves_tokens_and_cost_before_invocation():
    workflow = FakeWorkflowStore()
    guard = WorkflowExecutionGuard(
        workflow_store=workflow,
        closed_loop_store=FakeKillSwitchStore(),
    )

    result = guard.authorize_model_call(
        tenant_id="tenant-a",
        run_id="run-123",
        estimated_tokens=1200,
        estimated_cost_usd=0.04,
    )

    assert result == {"tokens_consumed": 1200.0, "cost_usd_consumed": 0.04}
    assert workflow.calls == [
        ("tenant-a", "run-123", "tokens", 1200),
        ("tenant-a", "run-123", "cost_usd", 0.04),
    ]
