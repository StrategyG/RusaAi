from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.agent_registry import DEPLOY_AGENT, OPERATE_AGENT
from rusa_agents.devops.artifacts import ContentAddressedArtifactStore
from rusa_agents.devops.deploy import (
    DeployAgentExecutor,
    DeploymentCheckResult,
    DeploymentExecutionResult,
    DeploymentSnapshot,
    DeploymentTarget,
    DeploymentVerificationResult,
    DeployPolicy,
    RollbackExecutionResult,
    RollbackRehearsalResult,
)
from rusa_agents.devops.release import ApprovalDecision
from rusa_agents.devops.specialists import ExecutionLease

TENANT = "tenant-deploy-test"
WORKFLOW = "workflow-deploy-test"
SOURCE_COMMIT = "a" * 40
ARTIFACT_DIGEST = "sha256:" + "b" * 64
PREVIOUS_DIGEST = "sha256:" + "c" * 64
REGISTRY_HOST = "registry.example"
ARTIFACT_REF = f"oci://{REGISTRY_HOST}/rusa/rusa-ai@{ARTIFACT_DIGEST}"
TARGET = DeploymentTarget("staging-blue", "staging", frozenset({"rusa-api"}))


class FakeSigner:
    def __init__(self, producer_id: str, key: bytes):
        self.producer_id = producer_id
        self.kid = f"{producer_id}-deploy-test-key"
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


class FakeApprovalVerifier:
    def __init__(self, decision: ApprovalDecision):
        self.decision = decision
        self.calls = 0

    def verify_approval(self, approval_ref: str, **kwargs) -> ApprovalDecision:
        self.calls += 1
        assert approval_ref == "approval://approved/deploy/change-1"
        assert kwargs["stage"] == "deploy"
        return self.decision


class FakeDeploymentRunner:
    def __init__(
        self,
        *,
        smoke_status: str = "passed",
        health_status: str = "passed",
        current_digest: str = PREVIOUS_DIGEST,
        deployed_digest: str = ARTIFACT_DIGEST,
        rehearsal_status: str = "passed",
        rollback_status: str = "passed",
    ):
        self.smoke_status = smoke_status
        self.health_status = health_status
        self.current_digest = current_digest
        self.deployed_digest = deployed_digest
        self.rehearsal_status = rehearsal_status
        self.rollback_status = rollback_status
        self.calls = {name: 0 for name in (
            "inspect", "rehearse", "deploy", "health", "smoke", "rollback"
        )}

    async def inspect_current(self, **kwargs):
        self.calls["inspect"] += 1
        return DeploymentSnapshot(
            kwargs["target"].target_id, kwargs["service"], self.current_digest
        )

    async def rehearse_rollback(self, **kwargs):
        self.calls["rehearse"] += 1
        now = datetime.now(timezone.utc)
        return RollbackRehearsalResult(
            kwargs["target"].target_id,
            kwargs["service"],
            kwargs["previous_digest"],
            self.rehearsal_status,
            now - timedelta(seconds=1),
            now,
        )

    async def deploy(self, **kwargs):
        self.calls["deploy"] += 1
        now = datetime.now(timezone.utc)
        previous = self.current_digest
        self.current_digest = self.deployed_digest
        return DeploymentExecutionResult(
            "deployment-123",
            kwargs["target"].target_id,
            kwargs["service"],
            self.deployed_digest,
            previous,
            now - timedelta(seconds=2),
            now,
        )

    async def health_check(self, **kwargs):
        self.calls["health"] += 1
        now = datetime.now(timezone.utc)
        return DeploymentVerificationResult(
            kwargs["target"].target_id,
            kwargs["service"],
            kwargs["deployment_id"],
            kwargs["expected_digest"],
            (DeploymentCheckResult("http_health", self.health_status, 0.2),),
            now - timedelta(seconds=1),
            now,
        )

    async def smoke_test(self, **kwargs):
        self.calls["smoke"] += 1
        now = datetime.now(timezone.utc)
        checks = tuple(
            DeploymentCheckResult(
                row["id"],
                self.smoke_status if row["id"] == "api_smoke" else "passed",
                0.1,
            )
            for row in kwargs["test_plan"]["checks"]
        )
        return DeploymentVerificationResult(
            kwargs["target"].target_id,
            kwargs["service"],
            kwargs["deployment_id"],
            kwargs["expected_digest"],
            checks,
            now - timedelta(seconds=1),
            now,
        )

    async def rollback(self, **kwargs):
        self.calls["rollback"] += 1
        now = datetime.now(timezone.utc)
        if self.rollback_status == "passed":
            self.current_digest = kwargs["previous_digest"]
        return RollbackExecutionResult(
            kwargs["target"].target_id,
            kwargs["service"],
            kwargs["deployment_id"],
            kwargs["previous_digest"] if self.rollback_status == "passed" else self.current_digest,
            self.rollback_status,
            now - timedelta(seconds=1),
            now,
        )


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _signed(store, tenant_id, payload, signer):
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


def _task(*, approval_required: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    constraints = {
        "allowed_tools": [
            "artifact.read", "release.read", "deployment.execute",
            "health.read", "runbook.read", "evidence.write",
        ],
        "network_access": False,
        "allowed_network_destinations": [],
        "write_scope": ["deployments/", "evidence/"],
        "approval_required": approval_required,
        "approval_scope": ["deploy"] if approval_required else [],
        "budget": {
            "max_cost_usd": 2.0,
            "max_tool_calls": 20,
            "max_runtime_seconds": 300,
        },
    }
    if approval_required:
        constraints["approval_ref"] = "approval://approved/deploy/change-1"
    return {
        "schema_version": 1,
        "task_id": f"deploy-{uuid4().hex[:10]}",
        "workflow_id": WORKFLOW,
        "tenant_id": TENANT,
        "agent_id": DEPLOY_AGENT,
        "stage": "deploy",
        "objective": "Deploy a verified release candidate to staging.",
        "input": {
            "release_manifest_ref": "artifact://pending/manifest",
            "release_evidence_ref": "evidence://pending/release",
            "environment": "staging",
            "target_id": TARGET.target_id,
            "service": "rusa-api",
            "commit_sha": SOURCE_COMMIT,
            "artifact_digest": ARTIFACT_DIGEST,
            "rollback_plan_ref": "artifact://pending/rollback",
            "smoke_test_plan_ref": "artifact://pending/smoke",
        },
        "constraints": constraints,
        "attempt": 1,
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
        "audit": {
            "created_at": now.isoformat(),
            "requested_by": "pytest",
            "policy_version": "deploy-policy-test-v1",
        },
    }


def _lease(task: dict) -> ExecutionLease:
    return ExecutionLease(
        tenant_id=task["tenant_id"],
        run_id=task["workflow_id"],
        task_id=task["task_id"],
        attempt=task["attempt"],
        principal=DEPLOY_AGENT,
        claim_id=f"claim-{uuid4().hex}",
        lease_epoch=1,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=2),
        accepted=True,
    )


def _seed_candidate(store, task, release_signer):
    tenant_id = task["tenant_id"]
    now = datetime.now(timezone.utc).isoformat()
    sbom = b'{"spdxVersion":"SPDX-2.3","SPDXID":"SPDXRef-DOCUMENT"}'
    sbom_ref = store.put(tenant_id, sbom, namespace="artifact")
    sbom_digest = hashlib.sha256(sbom).hexdigest()
    build_ref = "evidence://sha256/" + "1" * 64
    test_ref = "evidence://sha256/" + "2" * 64
    manifest = {
        "schema_version": 1,
        "kind": "rusa.release.candidate",
        "status": "candidate",
        "release_version": "1.2.3-rc.1",
        "release_channel": "candidate",
        "repository": "StrategyG/RusaAi",
        "source_commit": SOURCE_COMMIT,
        "artifact_ref": ARTIFACT_REF,
        "artifact_digest": ARTIFACT_DIGEST,
        "sbom_ref": sbom_ref,
        "sbom_digest": sbom_digest,
        "build_evidence_ref": build_ref,
        "sbom_evidence_ref": "evidence://sha256/" + "3" * 64,
        "test_evidence_ref": test_ref,
        "test_report_ref": "evidence://sha256/" + "4" * 64,
        "test_report_digest": "5" * 64,
        "changelog_ref": "artifact://sha256/" + "6" * 64,
        "migration_notes_ref": "artifact://sha256/" + "7" * 64,
        "known_issues_ref": None,
        "security_evidence_ref": None,
        "approval_ref": "approval://approved/release/change-1",
        "approval_id": "release-approval-1",
        "created_at": now,
        "policy_version": "release-policy-test-v1",
    }
    manifest_ref = store.put_json(tenant_id, manifest, namespace="artifact")
    manifest_digest = hashlib.sha256(store.get(tenant_id, manifest_ref)).hexdigest()
    release_payload = {
        "schema_version": 1,
        "type": "rusa.devops.release.attestation",
        "tenant_id": tenant_id,
        "workflow_id": task["workflow_id"],
        "task_id": "release-task-1",
        "attempt": 1,
        "agent_id": "rusa_release_agent",
        "release_version": manifest["release_version"],
        "release_manifest_ref": manifest_ref,
        "release_manifest_digest": manifest_digest,
        "readiness_report_ref": "artifact://sha256/" + "8" * 64,
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": ARTIFACT_DIGEST,
        "build_evidence_ref": build_ref,
        "test_evidence_ref": test_ref,
        "sbom_ref": sbom_ref,
        "sbom_digest": sbom_digest,
        "approval_ref": manifest["approval_ref"],
        "approval_id": manifest["approval_id"],
        "created_at": now,
        "policy_version": "release-policy-test-v1",
    }
    evidence_ref = _signed(store, tenant_id, release_payload, release_signer)
    rollback_ref = store.put_json(
        tenant_id,
        {
            "schema_version": 1,
            "strategy": "previous_digest",
            "expected_previous_digest": PREVIOUS_DIGEST,
            "max_recovery_seconds": 300,
        },
        namespace="artifact",
    )
    smoke_ref = store.put_json(
        tenant_id,
        {
            "schema_version": 1,
            "network_access": False,
            "checks": [
                {"id": "api_smoke", "required": True},
                {"id": "database_readiness", "required": True},
            ],
        },
        namespace="artifact",
    )
    task["input"].update(
        release_manifest_ref=manifest_ref,
        release_evidence_ref=evidence_ref,
        rollback_plan_ref=rollback_ref,
        smoke_test_plan_ref=smoke_ref,
    )


def _approval(task):
    now = datetime.now(timezone.utc)
    return ApprovalDecision(
        approved=True,
        tenant_id=task["tenant_id"],
        workflow_id=task["workflow_id"],
        stage="deploy",
        source_commit=SOURCE_COMMIT,
        artifact_digest=ARTIFACT_DIGEST,
        approval_id="deploy-approval-123",
        approver_id="human-approver-1",
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
    )


def _executor(tmp_path, runner, task, *, auto_rollback=False):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    release_signer = FakeSigner("rusa_release_agent", b"release-test-key")
    deploy_signer = FakeSigner(DEPLOY_AGENT, b"deploy-test-key")
    _seed_candidate(store, task, release_signer)
    verifier = FakeVerifier({
        "rusa_release_agent": release_signer.key,
        DEPLOY_AGENT: deploy_signer.key,
    })
    policy = DeployPolicy(
        allowed_targets={TARGET.target_id: TARGET},
        require_staging_approval=True,
        required_health_checks=frozenset({"http_health"}),
        required_smoke_checks=frozenset({"api_smoke"}),
        auto_rollback_on_failed_verification=auto_rollback,
    )
    approval_verifier = FakeApprovalVerifier(_approval(task))
    executor = DeployAgentExecutor(
        artifact_store=store,
        runner=runner,
        signer=deploy_signer,
        verifier=verifier,
        allowed_registry_hosts={REGISTRY_HOST},
        policy=policy,
        approval_verifier=approval_verifier,
    )
    return store, executor, deploy_signer, verifier, approval_verifier


@pytest.mark.asyncio
async def test_deploy_agent_verifies_release_and_emits_signed_staging_record(tmp_path):
    task = _task()
    runner = FakeDeploymentRunner()
    store, executor, signer, verifier, approval = _executor(tmp_path, runner, task)

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "passed"
    assert outcome.result["audit"]["source_commit"] == SOURCE_COMMIT
    assert outcome.result["audit"]["artifact_digest"] == ARTIFACT_DIGEST
    assert outcome.result["handoff"] == {
        "next_agent": OPERATE_AGENT, "event": "deploy.completed"
    }
    assert runner.calls["deploy"] == 1
    assert runner.calls["rehearse"] == 1
    assert runner.calls["rollback"] == 0
    assert approval.calls == 1
    record_ref = outcome.result["output"]["artifact_refs"][0]
    record = store.get_json(task["tenant_id"], record_ref)
    assert record["target_id"] == TARGET.target_id
    assert record["artifact_digest"] == ARTIFACT_DIGEST
    assert record["rollback_rehearsal"]["status"] == "passed"
    evidence_ref = outcome.result["output"]["evidence_refs"][0]
    envelope = store.get_json(task["tenant_id"], evidence_ref)
    assert envelope["producer_id"] == DEPLOY_AGENT
    assert envelope["payload"]["deployment_record_ref"] == record_ref
    assert verifier.verify(_canonical(envelope["payload"]), envelope["signature"]) == DEPLOY_AGENT
    assert signer.producer_id == DEPLOY_AGENT


@pytest.mark.asyncio
async def test_staging_approval_is_required_by_host_policy_before_runner_calls(tmp_path):
    task = _task(approval_required=False)
    runner = FakeDeploymentRunner()
    _, executor, _, _, _ = _executor(tmp_path, runner, task)

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "needs_approval"
    assert outcome.result["output"]["approval_ref"].startswith("approval://pending/deploy/")
    assert not any(runner.calls.values())


@pytest.mark.asyncio
async def test_tampered_release_signature_blocks_deploy(tmp_path):
    task = _task()
    runner = FakeDeploymentRunner()
    store, executor, _, _, _ = _executor(tmp_path, runner, task)
    envelope = store.get_json(task["tenant_id"], task["input"]["release_evidence_ref"])
    envelope["signature"] = "rusa_release_agent.invalid"
    task["input"]["release_evidence_ref"] = store.put_json(
        task["tenant_id"], envelope, namespace="evidence"
    )

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "deploy.release_or_plan_invalid"
    assert runner.calls["deploy"] == 0


@pytest.mark.asyncio
async def test_non_allowlisted_target_is_blocked_before_runner_call(tmp_path):
    task = _task()
    task["input"]["target_id"] = "staging-untrusted"
    runner = FakeDeploymentRunner()
    _, executor, _, _, _ = _executor(tmp_path, runner, task)

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "deploy.target_not_allowlisted"
    assert not any(runner.calls.values())


@pytest.mark.asyncio
async def test_rollback_plan_must_match_live_previous_digest(tmp_path):
    task = _task()
    runner = FakeDeploymentRunner(current_digest="sha256:" + "d" * 64)
    _, executor, _, _, _ = _executor(tmp_path, runner, task)

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "deploy.rollback_target_mismatch"
    assert runner.calls["rehearse"] == 0
    assert runner.calls["deploy"] == 0


@pytest.mark.asyncio
async def test_required_smoke_check_skipped_fails_and_prevents_handoff(tmp_path):
    task = _task()
    runner = FakeDeploymentRunner(smoke_status="skipped")
    _, executor, _, _, _ = _executor(tmp_path, runner, task)

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "failed"
    assert outcome.result["output"]["findings"][0]["code"] == "deploy.required_smoke_check_skipped"
    assert "handoff" not in outcome.result
    assert outcome.side_effect == "APPLIED"
    assert runner.current_digest == ARTIFACT_DIGEST


@pytest.mark.asyncio
async def test_failed_smoke_check_can_trigger_host_authorized_rollback(tmp_path):
    task = _task()
    runner = FakeDeploymentRunner(smoke_status="failed")
    store, executor, _, _, _ = _executor(
        tmp_path, runner, task, auto_rollback=True
    )

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "failed"
    assert runner.calls["rollback"] == 1
    assert runner.current_digest == PREVIOUS_DIGEST
    assert outcome.side_effect == "APPLIED"
    record = store.get_json(task["tenant_id"], outcome.result["output"]["artifact_refs"][0])
    assert record["rollback_execution"]["status"] == "passed"


@pytest.mark.asyncio
async def test_deploy_agent_fails_closed_when_health_check_observes_wrong_digest(tmp_path):
    task = _task()
    runner = FakeDeploymentRunner()
    original = runner.health_check

    async def wrong_digest_health(**kwargs):
        result = await original(**kwargs)
        return DeploymentVerificationResult(
            result.target_id, result.service, result.deployment_id,
            "sha256:" + "e" * 64, result.checks, result.started_at, result.completed_at,
        )

    runner.health_check = wrong_digest_health
    _, executor, _, _, _ = _executor(tmp_path, runner, task)

    outcome = await executor.execute(
        task, authenticated_principal=DEPLOY_AGENT, lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "failed"
    assert outcome.result["output"]["findings"][0]["code"] == "deploy.execution_failed"
    assert outcome.side_effect == "UNKNOWN"
