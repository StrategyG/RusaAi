from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.agent_registry import (
    AGENT_POLICIES,
    BUILD_AGENT,
    DEPLOY_AGENT,
    RELEASE_AGENT,
    TEST_AGENT,
)
from rusa_agents.devops.artifacts import ContentAddressedArtifactStore
from rusa_agents.devops.release import ApprovalDecision, ReleaseAgentExecutor, ReleasePolicy
from rusa_agents.devops.specialists import ExecutionLease


REGISTRY_HOST = "registry.example"
SOURCE_COMMIT = "a" * 40
ARTIFACT_DIGEST = "sha256:" + "b" * 64
ARTIFACT_REF = f"oci://{REGISTRY_HOST}/rusa/rusa-ai@{ARTIFACT_DIGEST}"
SBOM_BYTES = b'{"spdxVersion":"SPDX-2.3","SPDXID":"SPDXRef-DOCUMENT","name":"rusa-ai"}'


class FakeSigner:
    def __init__(self, producer_id: str, key: bytes):
        self.producer_id = producer_id
        self.kid = f"{producer_id}-release-test-key"
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
        assert approval_ref == "approval://approved/release/change-1"
        assert kwargs["stage"] == "release"
        return self.decision


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
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


def _task(tenant_id: str, workflow_id: str, *, approval_required: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    constraints = {
        "allowed_tools": sorted(AGENT_POLICIES[RELEASE_AGENT].allowed_tools),
        "network_access": False,
        "allowed_network_destinations": [],
        "write_scope": ["release/", "manifests/", "evidence/"],
        "approval_required": approval_required,
        "approval_scope": ["release"] if approval_required else [],
        "budget": {
            "max_cost_usd": 1.0,
            "max_tool_calls": 10,
            "max_runtime_seconds": 120,
        },
    }
    if approval_required:
        constraints["approval_ref"] = "approval://approved/release/change-1"
    return {
        "schema_version": 1,
        "task_id": f"release-{uuid4().hex[:10]}",
        "workflow_id": workflow_id,
        "tenant_id": tenant_id,
        "agent_id": RELEASE_AGENT,
        "stage": "release",
        "objective": "Verify release gates and prepare an immutable release candidate.",
        "input": {
            "commit_sha": SOURCE_COMMIT,
            "artifact_digest": ARTIFACT_DIGEST,
            "build_evidence_ref": "evidence://sha256/" + "1" * 64,
            "test_evidence_ref": "evidence://sha256/" + "2" * 64,
            "sbom_ref": "artifact://sha256/" + "3" * 64,
            "release_version": "1.2.3-rc.1",
            "changelog_ref": "artifact://sha256/" + "4" * 64,
            "migration_notes_ref": "artifact://sha256/" + "5" * 64,
        },
        "constraints": constraints,
        "attempt": 1,
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
        "audit": {
            "created_at": now.isoformat(),
            "requested_by": "release-test",
            "policy_version": "release-policy-test-v1",
        },
    }


def _seed_release_evidence(store, task, build_signer, test_signer):
    tenant_id = task["tenant_id"]
    workflow_id = task["workflow_id"]
    sbom_ref = store.put(tenant_id, SBOM_BYTES, namespace="artifact")
    sbom_digest = hashlib.sha256(SBOM_BYTES).hexdigest()
    build_task_id = "build-task-" + uuid4().hex[:8]
    common = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "task_id": build_task_id,
        "attempt": 1,
        "agent_id": BUILD_AGENT,
        "repository": "StrategyG/RusaAi",
        "source_commit": SOURCE_COMMIT,
        "artifact_ref": ARTIFACT_REF,
        "artifact_digest": ARTIFACT_DIGEST,
        "sbom_ref": sbom_ref,
        "sbom_digest": sbom_digest,
        "sbom_format": "spdx-json",
        "build_config_ref": "artifact://sha256/" + "a" * 64,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy_version": task["audit"]["policy_version"],
    }
    sbom_payload = {**common, "type": "rusa.devops.sbom.attestation"}
    sbom_evidence_ref = _signed(store, tenant_id, sbom_payload, build_signer)
    build_payload = {
        **common,
        "type": "rusa.devops.build.attestation",
        "sbom_evidence_ref": sbom_evidence_ref,
        "build_metadata": {"reproducibility_verified": True},
    }
    build_evidence_ref = _signed(store, tenant_id, build_payload, build_signer)
    test_report = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "task_id": "test-task-" + uuid4().hex[:8],
        "attempt": 1,
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": ARTIFACT_DIGEST,
        "artifact_ref": ARTIFACT_REF,
        "build_evidence_ref": build_evidence_ref,
        "test_plan_ref": "artifact://sha256/" + "6" * 64,
        "acceptance_criteria_ref": "artifact://sha256/" + "7" * 64,
        "runner": "isolated-test-runner",
        "runner_version": "1.0.0",
        "environment": "sandbox",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "suites": [
            {"suite_id": "unit", "status": "passed", "passed": 5, "failed": 0, "skipped": 0, "duration_seconds": 1.0},
            {"suite_id": "contract", "status": "passed", "passed": 3, "failed": 0, "skipped": 0, "duration_seconds": 0.5},
        ],
        "status": "passed",
    }
    test_report_ref = store.put_json(tenant_id, test_report, namespace="evidence")
    test_report_digest = hashlib.sha256(store.get(tenant_id, test_report_ref)).hexdigest()
    test_payload = {
        "schema_version": 1,
        "type": "rusa.devops.test.attestation",
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "task_id": test_report["task_id"],
        "attempt": 1,
        "agent_id": TEST_AGENT,
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": ARTIFACT_DIGEST,
        "artifact_ref": ARTIFACT_REF,
        "build_evidence_ref": build_evidence_ref,
        "sbom_evidence_ref": sbom_evidence_ref,
        "sbom_ref": sbom_ref,
        "sbom_digest": sbom_digest,
        "test_plan_ref": test_report["test_plan_ref"],
        "acceptance_criteria_ref": test_report["acceptance_criteria_ref"],
        "test_report_ref": test_report_ref,
        "test_report_digest": test_report_digest,
        "status": "passed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy_version": task["audit"]["policy_version"],
    }
    test_evidence_ref = _signed(store, tenant_id, test_payload, test_signer)
    task["input"]["build_evidence_ref"] = build_evidence_ref
    task["input"]["test_evidence_ref"] = test_evidence_ref
    task["input"]["sbom_ref"] = sbom_ref
    task["input"]["changelog_ref"] = store.put(
        tenant_id, b"## 1.2.3-rc.1\n\n- Verified candidate.\n", namespace="artifact"
    )
    task["input"]["migration_notes_ref"] = store.put(
        tenant_id, b"No database migration in this release.\n", namespace="artifact"
    )


def _lease(task):
    return ExecutionLease(
        tenant_id=task["tenant_id"],
        run_id=task["workflow_id"],
        task_id=task["task_id"],
        attempt=task["attempt"],
        principal=RELEASE_AGENT,
        claim_id="claim-" + uuid4().hex,
        lease_epoch=1,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=2),
        accepted=True,
    )


def _executor(tmp_path, approval_verifier):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    build_signer = FakeSigner(BUILD_AGENT, b"build-key")
    test_signer = FakeSigner(TEST_AGENT, b"test-key")
    release_signer = FakeSigner(RELEASE_AGENT, b"release-key")
    verifier = FakeVerifier({BUILD_AGENT: build_signer.key, TEST_AGENT: test_signer.key})
    executor = ReleaseAgentExecutor(
        artifact_store=store,
        signer=release_signer,
        verifier=verifier,
        allowed_registry_hosts={REGISTRY_HOST},
        approval_verifier=approval_verifier,
        policy=ReleasePolicy(required_test_suites=frozenset({"unit", "contract"})),
    )
    return store, executor, build_signer, test_signer, release_signer, verifier


def _approval(task):
    now = datetime.now(timezone.utc)
    return ApprovalDecision(
        approved=True,
        tenant_id=task["tenant_id"],
        workflow_id=task["workflow_id"],
        stage="release",
        source_commit=SOURCE_COMMIT,
        artifact_digest=ARTIFACT_DIGEST,
        approval_id="approval-record-1",
        approver_id="release-manager",
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
    )


@pytest.mark.asyncio
async def test_release_agent_verifies_signed_build_test_sbom_and_creates_candidate(tmp_path):
    task = _task("tenant-release", "workflow-release")
    store, executor, build_signer, test_signer, release_signer, verifier = _executor(
        tmp_path, FakeApprovalVerifier(_approval(task))
    )
    _seed_release_evidence(store, task, build_signer, test_signer)
    outcome = await executor.execute(
        task, authenticated_principal=RELEASE_AGENT, lease=_lease(task), checkpoint=lambda: True
    )
    assert outcome.result["status"] == "passed"
    assert outcome.result["audit"]["source_commit"] == SOURCE_COMMIT
    assert outcome.result["audit"]["artifact_digest"] == ARTIFACT_DIGEST
    assert outcome.result["handoff"] == {"next_agent": DEPLOY_AGENT, "event": "release.ready"}
    manifest_ref, report_ref = outcome.result["output"]["artifact_refs"]
    manifest = store.get_json(task["tenant_id"], manifest_ref)
    assert manifest["kind"] == "rusa.release.candidate"
    assert manifest["status"] == "candidate"
    assert manifest["release_version"] == "1.2.3-rc.1"
    assert manifest["approval_id"] == "approval-record-1"
    evidence_ref = outcome.result["output"]["evidence_refs"][0]
    envelope = store.get_json(task["tenant_id"], evidence_ref)
    assert envelope["producer_id"] == RELEASE_AGENT
    assert verifier.verify(_canonical(envelope["payload"]), envelope["signature"]) == RELEASE_AGENT
    assert envelope["payload"]["release_manifest_ref"] == manifest_ref
    assert store.get(task["tenant_id"], report_ref).startswith(b"# Release readiness:")
    assert release_signer.producer_id == RELEASE_AGENT


@pytest.mark.asyncio
async def test_release_agent_waits_for_approval_before_preparing_candidate(tmp_path):
    task = _task("tenant-release", "workflow-release")
    task["constraints"].pop("approval_ref")
    store, executor, build_signer, test_signer, _, _ = _executor(tmp_path, None)
    _seed_release_evidence(store, task, build_signer, test_signer)
    outcome = await executor.execute(
        task, authenticated_principal=RELEASE_AGENT, lease=_lease(task), checkpoint=lambda: True
    )
    assert outcome.result["status"] == "needs_approval"
    assert outcome.result["output"]["approval_ref"].startswith("approval://pending/release/")
    assert not outcome.result["output"]["artifact_refs"]


@pytest.mark.asyncio
async def test_release_agent_blocks_mismatched_approval(tmp_path):
    task = _task("tenant-release", "workflow-release")
    now = datetime.now(timezone.utc)
    invalid = ApprovalDecision(
        approved=True,
        tenant_id=task["tenant_id"],
        workflow_id=task["workflow_id"],
        stage="release",
        source_commit="c" * 40,
        artifact_digest=ARTIFACT_DIGEST,
        approval_id="approval-record-2",
        approver_id="release-manager",
        issued_at=now - timedelta(minutes=10),
        expires_at=now + timedelta(minutes=10),
    )
    store, executor, build_signer, test_signer, _, _ = _executor(
        tmp_path, FakeApprovalVerifier(invalid)
    )
    _seed_release_evidence(store, task, build_signer, test_signer)
    outcome = await executor.execute(
        task, authenticated_principal=RELEASE_AGENT, lease=_lease(task), checkpoint=lambda: True
    )
    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "release.approval_invalid"


@pytest.mark.asyncio
async def test_release_agent_rejects_missing_required_suite(tmp_path):
    task = _task("tenant-release", "workflow-release", approval_required=False)
    store, executor, build_signer, test_signer, _, _ = _executor(tmp_path, None)
    _seed_release_evidence(store, task, build_signer, test_signer)
    old_evidence_ref = task["input"]["test_evidence_ref"]
    envelope = store.get_json(task["tenant_id"], old_evidence_ref)
    payload = dict(envelope["payload"])
    report_ref = payload["test_report_ref"]
    report = store.get_json(task["tenant_id"], report_ref)
    report["suites"] = [report["suites"][0]]
    report_ref = store.put_json(task["tenant_id"], report, namespace="evidence")
    payload["test_report_ref"] = report_ref
    payload["test_report_digest"] = hashlib.sha256(store.get(task["tenant_id"], report_ref)).hexdigest()
    task["input"]["test_evidence_ref"] = _signed(store, task["tenant_id"], payload, test_signer)
    outcome = await executor.execute(
        task, authenticated_principal=RELEASE_AGENT, lease=_lease(task), checkpoint=lambda: True
    )
    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "release.gate_evidence_invalid"


def test_release_policy_rejects_invalid_gate_configuration():
    with pytest.raises(ValueError, match="required test suite"):
        ReleasePolicy(required_test_suites=frozenset())
