from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.closed_loop import (
    ClosedLoopConflictError,
    KillSwitchActiveError,
    PostgresClosedLoopStore,
    SLOAlert,
)

DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="RUSA_DATABASE_URL is not configured")


def _alert(tenant_id: str, message_id: str, observed_at: datetime) -> SLOAlert:
    return SLOAlert(
        tenant_id=tenant_id,
        message_id=message_id,
        service_id="rusa-api",
        environment="staging",
        rule_id="availability",
        severity="critical",
        summary="Availability SLO breached.",
        evidence_ref="evidence://sha256/" + "b" * 64,
        observed_at=observed_at,
        correlation_id="workflow-" + tenant_id,
        workflow_id="workflow-" + tenant_id,
        metric_name="availability",
        operator="gte",
        threshold=0.999,
        observed_value=0.98,
    )


def test_postgres_incident_dedup_lifecycle_reopen_and_kill_switches():
    store = PostgresClosedLoopStore(DSN)
    store.initialize()
    tenant_id = "tenant-" + uuid4().hex[:16]
    other_tenant = "tenant-" + uuid4().hex[:16]
    now = datetime.now(timezone.utc)

    first = store.record_slo_alerts([
        _alert(tenant_id, "monitor:" + uuid4().hex, now)
    ])[0]
    second_message_id = "monitor:" + uuid4().hex
    second = store.record_slo_alerts([
        _alert(tenant_id, second_message_id, now + timedelta(minutes=5))
    ])[0]
    assert first["incident_id"] == second["incident_id"]
    assert second["occurrence_count"] == 2
    assert second["correlated"] is True

    duplicate = store.record_slo_alerts([
        _alert(tenant_id, second_message_id, now + timedelta(minutes=5))
    ])[0]
    assert duplicate["duplicate"] is True
    assert duplicate["occurrence_count"] == 2

    with pytest.raises(ClosedLoopConflictError):
        store.record_slo_alerts([
            _alert(tenant_id, second_message_id, now + timedelta(minutes=6))
        ])

    acknowledged = store.transition_incident(
        tenant_id=tenant_id,
        incident_id=second["incident_id"],
        target_status="acknowledged",
        expected_state_version=second["state_version"],
        actor="rusa_operate_agent",
        message_id="operate:ack:" + uuid4().hex,
        evidence_ref="artifact://sha256/" + "c" * 64,
    )
    assert acknowledged["status"] == "acknowledged"

    with pytest.raises(ClosedLoopConflictError):
        store.transition_incident(
            tenant_id=tenant_id,
            incident_id=second["incident_id"],
            target_status="resolved",
            expected_state_version=second["state_version"],
            actor="rusa_operate_agent",
            message_id="operate:stale:" + uuid4().hex,
            evidence_ref="artifact://sha256/" + "d" * 64,
        )

    resolved = store.transition_incident(
        tenant_id=tenant_id,
        incident_id=second["incident_id"],
        target_status="resolved",
        expected_state_version=acknowledged["state_version"],
        actor="rusa_operate_agent",
        message_id="operate:resolve:" + uuid4().hex,
        evidence_ref="artifact://sha256/" + "e" * 64,
    )
    assert resolved["status"] == "resolved"

    reopened = store.record_slo_alerts([
        _alert(tenant_id, "monitor:" + uuid4().hex, now + timedelta(minutes=10))
    ])[0]
    assert reopened["incident_id"] == first["incident_id"]
    assert reopened["status"] == "open"
    assert reopened["resolved_at"] is None
    assert reopened["resolved_by"] is None
    assert reopened["occurrence_count"] == 3
    assert store.get_incident(other_tenant, reopened["incident_id"]) is None

    enabled = store.set_kill_switch(
        tenant_id=tenant_id,
        scope="deploy",
        enabled=True,
        reason_code="maintenance_window",
        actor="tenant_admin",
        expected_version=0,
        message_id="kill-switch:on:" + uuid4().hex,
    )
    assert enabled["version"] == 1
    with pytest.raises(KillSwitchActiveError):
        store.assert_action_enabled(tenant_id, "deploy")
    store.assert_action_enabled(other_tenant, "deploy")

    disabled = store.set_kill_switch(
        tenant_id=tenant_id,
        scope="deploy",
        enabled=False,
        reason_code="maintenance_complete",
        actor="tenant_admin",
        expected_version=1,
        message_id="kill-switch:off:" + uuid4().hex,
    )
    assert disabled["version"] == 2
    store.assert_action_enabled(tenant_id, "deploy")

    store.set_kill_switch(
        tenant_id="platform",
        scope="all",
        enabled=True,
        reason_code="platform_emergency",
        actor="platform_admin",
        expected_version=0,
        message_id="kill-switch:platform-on:" + uuid4().hex,
    )
    with pytest.raises(KillSwitchActiveError):
        store.assert_action_enabled(other_tenant, "monitor")
    store.set_kill_switch(
        tenant_id="platform",
        scope="all",
        enabled=False,
        reason_code="platform_recovered",
        actor="platform_admin",
        expected_version=1,
        message_id="kill-switch:platform-off:" + uuid4().hex,
    )
