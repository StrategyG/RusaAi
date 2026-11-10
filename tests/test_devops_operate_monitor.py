from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.agent_registry import (
    AGENT_POLICIES,
    DEPLOY_AGENT,
    MONITOR_AGENT,
    OPERATE_AGENT,
    SECURITY_GUARD_AGENT,
    AgentAuthorizationError,
)
from rusa_agents.devops.artifacts import ContentAddressedArtifactStore
from rusa_agents.devops.deploy import DeploymentTarget
from rusa_agents.devops.operate_monitor import (
    DependencyStatus,
    MonitorAgentExecutor,
    MonitorPolicy,
    MonitoringRecordWriter,
    OperateActionPolicy,
    OperateAgentExecutor,
    OperatePolicy,
    OperationalSnapshot,
    RunbookActionResult,
    SecurityAssessmentResult,
    SecurityFinding,
    SecurityGuardExecutor,
    TelemetrySnapshot,
)
from rusa_agents.devops.release import ApprovalDecision
from rusa_agents.devops.specialists import ExecutionLease
from rusa_agents.policy import AutonomyPolicy, RiskLevel

TENANT = "tenant-operations-test"
WORKFLOW = "workflow-operations-test"
SERVICE = "rusa-api"
TARGET = DeploymentTarget("staging-blue", "staging", frozenset({SERVICE}))
SOURCE_COMMIT = "a" * 40
ARTIFACT_DIGEST = "sha256:" + "b" * 64


class FakeSigner:
    def __init__(self, producer_id: str, key: bytes):
        self.producer_id = producer_id
        self.kid = f"{producer_id}-operations-test-key"
        self.key = key

    def sign(self, payload: bytes) -> str:
        digest = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return f"{self.producer_id}.{digest}"


class FakeVerifier:
    def __init__(self, keys: dict[str, bytes]):
        self.keys = keys

    def verify(self, payload: bytes, detached_jws: str) -> str:
        producer, separator, digest = detached_jws.partition(".")
        key = self.keys.get(producer)
        if not separator or key is None:
            raise ValueError("untrusted signature")
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            raise ValueError("signature mismatch")
        return producer


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _signed(store, tenant_id: str, payload: dict, signer: FakeSigner) -> str:
    return store.put_json(
        tenant_id,
        {
            "schema_version": 1,
            "producer_id": signer.producer_id,
            "payload": payload,
            "signature": signer.sign(_canonical(payload)),
        },
        namespace="evidence",
    )


def _seed_deployment(store, signer: FakeSigner) -> str:
    record = {
        "schema_version": 1,
        "type": "rusa.devops.deployment.record",
        "tenant_id": TENANT,
        "workflow_id": WORKFLOW,
        "environment": "staging",
        "target_id": TARGET.target_id,
        "service": SERVICE,
        "deployment_id": "deployment-operations-1",
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": ARTIFACT_DIGEST,
        "status": "passed",
    }
    record_ref = store.put_json(TENANT, record, namespace="artifact")
    payload = {
        "schema_version": 1,
        "type": "rusa.devops.deploy.attestation",
        "tenant_id": TENANT,
        "workflow_id": WORKFLOW,
        "task_id": "deploy-task-operations",
        "attempt": 1,
        "agent_id": DEPLOY_AGENT,
        "environment": "staging",
        "target_id": TARGET.target_id,
        "service": SERVICE,
        "deployment_record_ref": record_ref,
        "deployment_record_digest": record_ref.rsplit("/", 1)[-1],
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": ARTIFACT_DIGEST,
        "deployment_id": record["deployment_id"],
        "status": "passed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy_version": "deploy-policy-test-v1",
    }
    return _signed(store, TENANT, payload, signer)


def _task(
    agent_id: str,
    stage: str,
    task_input: dict,
    *,
    write_scope: list[str],
    approval_required: bool = False,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "task_id": f"{stage}-{uuid4().hex[:12]}",
        "workflow_id": WORKFLOW,
        "tenant_id": TENANT,
        "agent_id": agent_id,
        "stage": stage,
        "objective": f"Execute bounded {stage} operations for the staging service.",
        "input": task_input,
        "constraints": {
            "allowed_tools": sorted(AGENT_POLICIES[agent_id].allowed_tools),
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": write_scope,
            "approval_required": approval_required,
            "approval_scope": ["operate"] if approval_required else [],
            "budget": {
                "max_cost_usd": 1.0,
                "max_tool_calls": 10,
                "max_runtime_seconds": 120,
            },
        },
        "attempt": 1,
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
        "audit": {
            "created_at": now.isoformat(),
            "requested_by": "pytest-operations",
            "policy_version": "operations-policy-test-v1",
        },
    }


def _lease(task: dict) -> ExecutionLease:
    return ExecutionLease(
        tenant_id=task["tenant_id"],
        run_id=task["workflow_id"],
        task_id=task["task_id"],
        attempt=task["attempt"],
        principal=task["agent_id"],
        claim_id=f"claim-{uuid4().hex}",
        lease_epoch=1,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=2),
        accepted=True,
    )


class FakeOperateRunner:
    def __init__(self):
        self.inspect_calls = 0
        self.action_calls = 0

    async def inspect_service(self, **kwargs):
        self.inspect_calls += 1
        return OperationalSnapshot(
            target_id=kwargs["target"].target_id,
            service=kwargs["service"],
            deployment_id=kwargs["deployment_id"],
            observed_digest=kwargs["expected_digest"],
            status="healthy",
            dependencies=(DependencyStatus("postgres", "healthy"),),
            cpu_percent=22.0,
            memory_percent=31.0,
            disk_percent=44.0,
            queue_depth=2,
            sampled_at=datetime.now(timezone.utc),
        )

    async def execute_action(self, **kwargs):
        self.action_calls += 1
        now = datetime.now(timezone.utc)
        return RunbookActionResult(
            action_id=kwargs["action_id"],
            status="passed",
            deployment_id=kwargs["deployment_id"],
            started_at=now - timedelta(seconds=1),
            completed_at=now,
        )


class FakeOperationalRecordWriter:
    def __init__(self):
        self.records = []

    async def write_operational_record(self, **kwargs):
        self.records.append(kwargs)
        return "report://operations/record-1"


class FakeMonitorRunner:
    def __init__(self, *, p95_latency_ms: float = 100.0, observed_digest: str = ARTIFACT_DIGEST):
        self.p95_latency_ms = p95_latency_ms
        self.observed_digest = observed_digest
        self.calls = 0

    async def collect_telemetry(self, **kwargs):
        self.calls += 1
        now = datetime.now(timezone.utc)
        return TelemetrySnapshot(
            target_id=kwargs["target"].target_id,
            service=kwargs["service"],
            deployment_id=kwargs["deployment_id"],
            observed_digest=self.observed_digest,
            sampled_at=now,
            window_start=now - timedelta(minutes=5),
            window_end=now,
            request_count=100,
            error_count=0,
            p95_latency_ms=self.p95_latency_ms,
            uptime_ratio=0.9999,
            queue_depth=2,
            cpu_percent=22.0,
            memory_percent=31.0,
            disk_percent=44.0,
        )


class FakeMonitoringRecordWriter:
    def __init__(self):
        self.records = []

    async def write_monitoring_record(self, **kwargs):
        self.records.append(kwargs)
        return "monitoring://reports/report-1"


class FakeSecurityRunner:
    def __init__(self, findings: tuple[SecurityFinding, ...]):
        self.findings = findings
        self.calls = 0

    async def assess(self, **kwargs):
        self.calls += 1
        assert len(kwargs["evidence_refs"]) == len(kwargs["evidence_digests"])
        assert all(len(digest) == 64 for digest in kwargs["evidence_digests"])
        return SecurityAssessmentResult("scanner-v1", self.findings)


def _deployment_context(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    deploy_signer = FakeSigner(DEPLOY_AGENT, b"deploy-operations-test-key")
    verifier = FakeVerifier({DEPLOY_AGENT: deploy_signer.key})
    deployment_ref = _seed_deployment(store, deploy_signer)
    return store, verifier, deployment_ref


@pytest.mark.asyncio
async def test_operate_executes_only_allowlisted_runbook_action_and_hands_off_to_monitor(tmp_path):
    store, verifier, deployment_ref = _deployment_context(tmp_path)
    runbook_ref = store.put_json(
        TENANT,
        {"schema_version": 1, "service": SERVICE, "actions": ["restart_service"]},
        namespace="artifact",
    )
    task = _task(
        OPERATE_AGENT,
        "operate",
        {
            "service": SERVICE,
            "deployment_evidence_ref": deployment_ref,
            "runbook_ref": runbook_ref,
            "action_id": "restart_service",
        },
        write_scope=["incidents/", "reports/"],
    )
    runner = FakeOperateRunner()
    writer = FakeOperationalRecordWriter()
    executor = OperateAgentExecutor(
        artifact_store=store,
        runner=runner,
        record_writer=writer,
        verifier=verifier,
        policy=OperatePolicy(
            allowed_targets={TARGET.target_id: TARGET},
            allowed_actions={
                "restart_service": OperateActionPolicy(
                    "restart_service", RiskLevel.LOW, requires_approval=False
                )
            },
            require_approval_for_mutations=False,
        ),
        autonomy_policy=AutonomyPolicy(allowed_actions={"restart_service"}),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "passed"
    assert outcome.result["handoff"] == {
        "next_agent": MONITOR_AGENT,
        "event": "operate.recovered",
    }
    assert runner.inspect_calls == 2
    assert runner.action_calls == 1
    assert writer.records[0]["record"]["artifact_digest"] == ARTIFACT_DIGEST
    assert writer.records[0]["record"]["action"]["action_id"] == "restart_service"


@pytest.mark.asyncio
async def test_operate_mutation_waits_for_human_approval_before_runner_call(tmp_path):
    store, verifier, deployment_ref = _deployment_context(tmp_path)
    runbook_ref = store.put_json(
        TENANT,
        {"schema_version": 1, "service": SERVICE, "actions": ["restart_service"]},
        namespace="artifact",
    )
    task = _task(
        OPERATE_AGENT,
        "operate",
        {
            "service": SERVICE,
            "deployment_evidence_ref": deployment_ref,
            "runbook_ref": runbook_ref,
            "action_id": "restart_service",
        },
        write_scope=["incidents/", "reports/"],
    )
    runner = FakeOperateRunner()
    executor = OperateAgentExecutor(
        artifact_store=store,
        runner=runner,
        record_writer=FakeOperationalRecordWriter(),
        verifier=verifier,
        policy=OperatePolicy(
            allowed_targets={TARGET.target_id: TARGET},
            allowed_actions={
                "restart_service": OperateActionPolicy(
                    "restart_service", RiskLevel.LOW, requires_approval=False
                )
            },
        ),
        autonomy_policy=AutonomyPolicy(allowed_actions={"restart_service"}),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=OPERATE_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "needs_approval"
    assert outcome.result["output"]["approval_ref"].startswith("approval://pending/operate/")
    assert runner.inspect_calls == 0
    assert runner.action_calls == 0


@pytest.mark.asyncio
async def test_monitor_is_read_only_and_routes_slo_breach_to_operate(tmp_path):
    store, verifier, deployment_ref = _deployment_context(tmp_path)
    profile_ref = store.put_json(
        TENANT,
        {
            "schema_version": 1,
            "availability_target": 0.999,
            "max_p95_latency_ms": 500,
            "max_error_rate": 0.01,
            "max_queue_depth": 100,
            "max_cpu_percent": 80,
            "max_memory_percent": 80,
            "max_disk_percent": 80,
        },
        namespace="artifact",
    )
    task = _task(
        MONITOR_AGENT,
        "monitor",
        {
            "service": SERVICE,
            "deployment_evidence_ref": deployment_ref,
            "slo_profile_ref": profile_ref,
            "metrics_window": "5m",
        },
        write_scope=[],
    )
    runner = FakeMonitorRunner(p95_latency_ms=1200)
    writer = FakeMonitoringRecordWriter()
    executor = MonitorAgentExecutor(
        artifact_store=store,
        runner=runner,
        record_writer=writer,
        verifier=verifier,
        policy=MonitorPolicy(allowed_targets={TARGET.target_id: TARGET}),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=MONITOR_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "passed"
    assert outcome.result["handoff"] == {
        "next_agent": OPERATE_AGENT,
        "event": "monitor.alert_triggered",
    }
    assert outcome.result["output"]["findings"][0]["code"] == "monitor.slo_latency_breach"
    assert writer.records[0]["record"]["status"] == "alert"
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_monitor_rejects_telemetry_for_a_different_deployment_digest(tmp_path):
    store, verifier, deployment_ref = _deployment_context(tmp_path)
    profile_ref = store.put_json(
        TENANT,
        {
            "schema_version": 1,
            "availability_target": 0.999,
            "max_p95_latency_ms": 500,
            "max_error_rate": 0.01,
            "max_queue_depth": 100,
            "max_cpu_percent": 80,
            "max_memory_percent": 80,
            "max_disk_percent": 80,
        },
        namespace="artifact",
    )
    task = _task(
        MONITOR_AGENT,
        "monitor",
        {
            "service": SERVICE,
            "deployment_evidence_ref": deployment_ref,
            "slo_profile_ref": profile_ref,
            "metrics_window": "5m",
        },
        write_scope=[],
    )
    runner = FakeMonitorRunner(observed_digest="sha256:" + "d" * 64)
    writer = FakeMonitoringRecordWriter()
    executor = MonitorAgentExecutor(
        artifact_store=store,
        runner=runner,
        record_writer=writer,
        verifier=verifier,
        policy=MonitorPolicy(allowed_targets={TARGET.target_id: TARGET}),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=MONITOR_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "failed"
    assert outcome.result["output"]["findings"][0]["code"] == "monitor.collection_invalid"
    assert writer.records == []


@pytest.mark.asyncio
async def test_security_guard_emits_signed_findings_without_write_capabilities(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    evidence_ref = store.put(TENANT, b"immutable deployment evidence", namespace="evidence")
    task = _task(
        SECURITY_GUARD_AGENT,
        "test",
        {
            "evidence_refs": [evidence_ref],
            "commit_sha": SOURCE_COMMIT,
            "artifact_digest": ARTIFACT_DIGEST,
        },
        write_scope=[],
    )
    signer = FakeSigner(SECURITY_GUARD_AGENT, b"security-guard-test-key")
    verifier = FakeVerifier({SECURITY_GUARD_AGENT: signer.key})
    runner = FakeSecurityRunner((
        SecurityFinding(
            "high",
            "secret.detected",
            "Detected api_key=supersecret and AKIA1234567890ABCDEF in evidence.",
            evidence_ref,
        ),
    ))
    executor = SecurityGuardExecutor(
        artifact_store=store,
        runner=runner,
        signer=signer,
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=SECURITY_GUARD_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.failure_category == "PERMANENT"
    assert outcome.side_effect == "APPLIED"
    assert runner.calls == 1
    signed_ref = outcome.result["output"]["evidence_refs"][0]
    envelope = store.get_json(TENANT, signed_ref)
    assert envelope["producer_id"] == SECURITY_GUARD_AGENT
    assert verifier.verify(_canonical(envelope["payload"]), envelope["signature"]) == SECURITY_GUARD_AGENT
    message = envelope["payload"]["findings"][0]["message"]
    assert "supersecret" not in message
    assert "AKIA1234567890ABCDEF" not in message
    assert "REDACTED" in message


@pytest.mark.asyncio
async def test_security_guard_invalid_evidence_uri_fails_closed(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    task = _task(
        SECURITY_GUARD_AGENT,
        "test",
        {
            "evidence_refs": ["https://untrusted.example/evidence.json"],
            "commit_sha": SOURCE_COMMIT,
            "artifact_digest": ARTIFACT_DIGEST,
        },
        write_scope=[],
    )
    signer = FakeSigner(SECURITY_GUARD_AGENT, b"security-guard-test-key")
    runner = FakeSecurityRunner(())
    executor = SecurityGuardExecutor(
        artifact_store=store,
        runner=runner,
        signer=signer,
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=SECURITY_GUARD_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "security.evidence_unavailable"
    assert runner.calls == 0


def test_monitor_and_security_guard_cannot_receive_write_scopes():
    monitor_task = _task(
        MONITOR_AGENT,
        "monitor",
        {
            "service": SERVICE,
            "deployment_evidence_ref": "evidence://sha256/" + "a" * 64,
            "slo_profile_ref": "artifact://sha256/" + "b" * 64,
            "metrics_window": "5m",
        },
        write_scope=["monitoring/"],
    )
    security_task = _task(
        SECURITY_GUARD_AGENT,
        "test",
        {"evidence_refs": ["evidence://sha256/" + "a" * 64]},
        write_scope=["evidence/"],
    )

    with pytest.raises(AgentAuthorizationError):
        from rusa_agents.devops.agent_registry import authorize_agent_task

        authorize_agent_task(monitor_task, authenticated_principal=MONITOR_AGENT)
    with pytest.raises(AgentAuthorizationError):
        from rusa_agents.devops.agent_registry import authorize_agent_task

        authorize_agent_task(security_task, authenticated_principal=SECURITY_GUARD_AGENT)
