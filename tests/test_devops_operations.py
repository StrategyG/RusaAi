from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.agent_registry import MONITOR_AGENT, MONITOR_READ_ONLY_TOOLS, OPERATE_AGENT
from rusa_agents.devops.artifacts import ContentAddressedArtifactStore
from rusa_agents.devops.operations import (
    MonitorAgentExecutor,
    OperateAgentExecutor,
    RunbookAction,
    RunbookDefinition,
    RunbookExecutionResult,
    SLORule,
    TelemetryFeedback,
    TelemetrySnapshot,
)
from rusa_agents.devops.specialists import ExecutionLease

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _task(
    *,
    agent_id: str,
    stage: str,
    allowed_tools: set[str] | frozenset[str],
    write_scope: list[str],
    task_input: dict,
    approval_required: bool = False,
    approval_scope: list[str] | None = None,
    approval_ref: str | None = None,
) -> dict:
    constraints = {
        "allowed_tools": sorted(allowed_tools),
        "network_access": False,
        "allowed_network_destinations": [],
        "write_scope": write_scope,
        "approval_required": approval_required,
        "budget": {
            "max_cost_usd": 2.0,
            "max_tool_calls": 20,
            "max_runtime_seconds": 60,
        },
    }
    if approval_scope is not None:
        constraints["approval_scope"] = approval_scope
    if approval_ref is not None:
        constraints["approval_ref"] = approval_ref
    return {
        "schema_version": 1,
        "task_id": f"task-{uuid4().hex[:12]}",
        "workflow_id": f"run-{uuid4().hex[:12]}",
        "tenant_id": "tenant-operations",
        "agent_id": agent_id,
        "stage": stage,
        "objective": "Verify bounded service operations and monitoring.",
        "input": task_input,
        "constraints": constraints,
        "attempt": 1,
        "deadline_at": (NOW + timedelta(minutes=5)).isoformat(),
        "audit": {
            "created_at": NOW.isoformat(),
            "requested_by": "pytest",
            "policy_version": "policy-test-v1",
            "traceparent": TRACEPARENT,
        },
    }


def _lease(task: dict, principal: str) -> ExecutionLease:
    return ExecutionLease(
        tenant_id=task["tenant_id"],
        run_id=task["workflow_id"],
        task_id=task["task_id"],
        attempt=task["attempt"],
        principal=principal,
        claim_id="claim-" + uuid4().hex,
        lease_epoch=1,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=2),
    )


class AllowPolicy:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.calls = []

    def authorize_operation(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return self.allowed


class ApprovalVerifier:
    def __init__(self, approved: bool):
        self.approved = approved
        self.calls = []

    def verify_approval(self, approval_ref: str, **kwargs) -> bool:
        self.calls.append((approval_ref, kwargs))
        return self.approved


class FakeRunbookRunner:
    def __init__(self, status: str = "passed", side_effect: str = "APPLIED"):
        self.status = status
        self.side_effect = side_effect
        self.calls = []

    async def execute(self, **kwargs) -> RunbookExecutionResult:
        self.calls.append(kwargs)
        return RunbookExecutionResult(
            status=self.status,
            summary="Service recovery action completed and health was verified.",
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=2),
            side_effect=self.side_effect,
            details={"observed_state": "healthy", "verification": "health_check_passed"},
        )


def _operate_fixture(tmp_path, *, destructive: bool = False):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    invocation = {
        "runbook_id": "service_recovery",
        "action_id": "restart_service" if destructive else "verify_health",
        "parameters": {"reason": "approved recovery check"},
    }
    runbook_ref = store.put_json("tenant-operations", invocation, namespace="artifact")
    action = RunbookAction(
        action_id=invocation["action_id"],
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["reason"],
            "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 128}},
        },
        destructive=destructive,
        approval_required=destructive,
        approval_scope="destructive_action" if destructive else None,
    )
    definition = RunbookDefinition(
        runbook_id="service_recovery",
        reference=runbook_ref,
        allowed_services=frozenset({"rusa-api"}),
        actions={action.action_id: action},
        max_execution_seconds=30,
    )
    task = _task(
        agent_id=OPERATE_AGENT,
        stage="operate",
        allowed_tools={
            "metrics.read", "logs.read", "runbook.read", "runbook.execute", "incident.write",
        },
        write_scope=["incidents/", "reports/"],
        task_input={
            "service": "rusa-api",
            "environment": "staging",
            "runbook_ref": runbook_ref,
        },
    )
    return store, definition, task


@pytest.mark.asyncio
async def test_operate_executes_only_registered_runbook_and_persists_incident_evidence(tmp_path):
    store, definition, task = _operate_fixture(tmp_path)
    runner = FakeRunbookRunner()
    policy = AllowPolicy()
    executor = OperateAgentExecutor(
        artifact_store=store,
        runbooks={definition.runbook_id: definition},
        runner=runner,
        policy_authorizer=policy,
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task, OPERATE_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "passed"
    assert outcome.side_effect == "APPLIED"
    assert outcome.result["handoff"] == {
        "next_agent": MONITOR_AGENT,
        "event": "operate.recovered",
    }
    assert outcome.result["output"]["artifact_refs"] == outcome.result["output"]["evidence_refs"]
    report = store.get_json(task["tenant_id"], outcome.result["output"]["artifact_refs"][0])
    assert report["runbook_id"] == "service_recovery"
    assert report["action_id"] == "verify_health"
    assert runner.calls[0]["service"] == "rusa-api"
    assert runner.calls[0]["action_id"] == "verify_health"
    assert "command" not in runner.calls[0]
    assert policy.calls[0]["risk"] == "low"


@pytest.mark.asyncio
async def test_operate_blocks_destructive_action_without_task_scoped_human_approval(tmp_path):
    store, definition, task = _operate_fixture(tmp_path, destructive=True)
    runner = FakeRunbookRunner()
    executor = OperateAgentExecutor(
        artifact_store=store,
        runbooks={definition.runbook_id: definition},
        runner=runner,
        policy_authorizer=AllowPolicy(),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task, OPERATE_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "operate.human_approval_required"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_operate_requires_exact_verified_approval_before_destructive_action(tmp_path):
    store, definition, task = _operate_fixture(tmp_path, destructive=True)
    task["constraints"]["approval_required"] = True
    task["constraints"]["approval_scope"] = ["destructive_action"]
    task["constraints"]["approval_ref"] = "approval://approved/restart-123"
    runner = FakeRunbookRunner()
    approval = ApprovalVerifier(approved=False)
    executor = OperateAgentExecutor(
        artifact_store=store,
        runbooks={definition.runbook_id: definition},
        runner=runner,
        policy_authorizer=AllowPolicy(),
        approval_verifier=approval,
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task, OPERATE_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "operate.approval_not_verified"
    assert approval.calls[0][1]["task_id"] == task["task_id"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_operate_runs_destructive_action_only_after_approved_binding(tmp_path):
    store, definition, task = _operate_fixture(tmp_path, destructive=True)
    task["constraints"]["approval_required"] = True
    task["constraints"]["approval_scope"] = ["destructive_action"]
    task["constraints"]["approval_ref"] = "approval://approved/restart-123"
    runner = FakeRunbookRunner()
    approval = ApprovalVerifier(approved=True)
    executor = OperateAgentExecutor(
        artifact_store=store,
        runbooks={definition.runbook_id: definition},
        runner=runner,
        policy_authorizer=AllowPolicy(),
        approval_verifier=approval,
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task, OPERATE_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "passed"
    assert len(runner.calls) == 1
    assert approval.calls[0][1]["resource_ref"] == "rusa-api"
    assert runner.calls[0]["action_id"] == "restart_service"


@pytest.mark.asyncio
async def test_operate_fails_closed_when_policy_engine_denies_action(tmp_path):
    store, definition, task = _operate_fixture(tmp_path)
    runner = FakeRunbookRunner()
    executor = OperateAgentExecutor(
        artifact_store=store,
        runbooks={definition.runbook_id: definition},
        runner=runner,
        policy_authorizer=AllowPolicy(allowed=False),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task, OPERATE_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "operate.policy_denied"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_operate_rejects_unregistered_runbook_action_before_runner_call(tmp_path):
    store, definition, task = _operate_fixture(tmp_path)
    task["input"]["runbook_ref"] = store.put_json(
        task["tenant_id"],
        {
            "runbook_id": "service_recovery",
            "action_id": "execute_shell",
            "parameters": {"reason": "not allowed"},
        },
        namespace="artifact",
    )
    runner = FakeRunbookRunner()
    executor = OperateAgentExecutor(
        artifact_store=store,
        runbooks={definition.runbook_id: definition},
        runner=runner,
        policy_authorizer=AllowPolicy(),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task, OPERATE_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert runner.calls == []


class FakeTelemetryReader:
    def __init__(self, snapshot: TelemetrySnapshot):
        self.snapshot = snapshot
        self.calls = []

    async def collect(self, **kwargs) -> TelemetrySnapshot:
        self.calls.append(kwargs)
        return self.snapshot


def _monitor_task(*, evidence_refs: list[str] | None = None) -> dict:
    return _task(
        agent_id=MONITOR_AGENT,
        stage="monitor",
        allowed_tools=MONITOR_READ_ONLY_TOOLS,
        write_scope=[],
        task_input={
            "service": "rusa-api",
            "environment": "staging",
            "metrics_window": "5m",
            "evidence_refs": evidence_refs or [],
        },
    )


@pytest.mark.asyncio
async def test_monitor_evaluates_slo_and_routes_alert_to_operate(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    telemetry_ref = store.put("tenant-operations", b"metrics-window-evidence", namespace="evidence")
    snapshot = TelemetrySnapshot(
        observed_at=NOW,
        metrics={"availability": 0.98, "p95_latency_ms": 210.0},
        log_count=15,
        trace_count=9,
        evidence_refs=(telemetry_ref,),
    )
    reader = FakeTelemetryReader(snapshot)
    executor = MonitorAgentExecutor(
        telemetry_reader=reader,
        rules_by_service={
            "rusa-api": (
                SLORule(
                    rule_id="availability",
                    metric_name="availability",
                    operator="gte",
                    threshold=0.999,
                    severity="critical",
                    description="API availability SLO is below target.",
                    recommendation="Inspect recent deployments and dependency health.",
                ),
                SLORule(
                    rule_id="latency",
                    metric_name="p95_latency_ms",
                    operator="lte",
                    threshold=300.0,
                    severity="high",
                    description="API latency is above target.",
                    recommendation="Inspect trace spans for slow dependencies.",
                ),
            )
        },
    )

    outcome = await executor.execute(
        _monitor_task(),
        authenticated_principal=MONITOR_AGENT,
        lease=_lease(_monitor_task(), MONITOR_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "passed"
    assert outcome.result["handoff"] == {
        "next_agent": OPERATE_AGENT,
        "event": "monitor.alert_triggered",
    }
    assert len(outcome.result["output"]["findings"]) == 1
    assert outcome.result["output"]["findings"][0]["severity"] == "critical"
    assert outcome.result["output"]["evidence_refs"] == [telemetry_ref]
    assert reader.calls[0]["window"] == "5m"


@pytest.mark.asyncio
async def test_monitor_routes_recurring_feedback_to_plan_without_mutating_services(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    telemetry_ref = store.put("tenant-operations", b"capacity-trend-evidence", namespace="evidence")
    snapshot = TelemetrySnapshot(
        observed_at=NOW,
        metrics={"availability": 0.9999},
        log_count=2,
        trace_count=1,
        evidence_refs=(telemetry_ref,),
        feedback=(
            TelemetryFeedback(
                code="capacity_trend",
                severity="medium",
                message="Queue utilization has increased across several windows.",
                recommendation="Plan a capacity review and load test.",
            ),
        ),
    )
    reader = FakeTelemetryReader(snapshot)
    executor = MonitorAgentExecutor(
        telemetry_reader=reader,
        rules_by_service={
            "rusa-api": (
                SLORule(
                    rule_id="availability",
                    metric_name="availability",
                    operator="gte",
                    threshold=0.999,
                    severity="critical",
                    description="API availability SLO is below target.",
                    recommendation="Inspect service health.",
                ),
            )
        },
    )
    task = _monitor_task()
    outcome = await executor.execute(
        task,
        authenticated_principal=MONITOR_AGENT,
        lease=_lease(task, MONITOR_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["handoff"] == {
        "next_agent": "rusa_plan_agent",
        "event": "monitor.feedback_created",
    }
    assert outcome.result["output"]["artifact_refs"] == []
    assert len(reader.calls) == 1


@pytest.mark.asyncio
async def test_monitor_blocks_unverifiable_telemetry_without_evidence(tmp_path):
    reader = FakeTelemetryReader(
        TelemetrySnapshot(
            observed_at=NOW,
            metrics={"availability": 0.9999},
            log_count=0,
            trace_count=0,
        )
    )
    executor = MonitorAgentExecutor(
        telemetry_reader=reader,
        rules_by_service={
            "rusa-api": (
                SLORule(
                    rule_id="availability",
                    metric_name="availability",
                    operator="gte",
                    threshold=0.999,
                    severity="critical",
                    description="API availability SLO is below target.",
                    recommendation="Inspect service health.",
                ),
            )
        },
    )
    task = _monitor_task()
    outcome = await executor.execute(
        task,
        authenticated_principal=MONITOR_AGENT,
        lease=_lease(task, MONITOR_AGENT),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "monitor.telemetry_evidence_missing"


def test_monitor_rejects_non_finite_metrics_and_read_only_scope_escalation():
    with pytest.raises(ValueError, match="finite"):
        TelemetrySnapshot(
            observed_at=NOW,
            metrics={"availability": float("nan")},
            log_count=0,
            trace_count=0,
        )

    task = _monitor_task()
    task["constraints"]["write_scope"] = ["incidents/"]
    with pytest.raises(PermissionError):
        MonitorAgentExecutor._validate_task(task, MONITOR_AGENT)
