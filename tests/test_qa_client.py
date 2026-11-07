"""Worker-side verification tests for signed independent QA verdicts."""
import base64
import io
import tarfile
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rusa_agents.contracts import CodingTask, RepositoryTarget
from rusa_agents.qa_attestation import REQUIRED_QA_PROFILES, digest_snapshot_archive, sign_verdict
from rusa_agents.qa_client import IndependentQAClient, QAAttestationError

SANDBOX_IMAGE = "registry.invalid/rusa-sandbox@sha256:" + "a" * 64


def make_archive():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("README.md")
        info.size = len(b"RUSA-AI")
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(b"RUSA-AI"))
    return buffer.getvalue()


class FakePublisher:
    def prepare_candidate_snapshot(self, workspace):
        return make_archive(), "c" * 40


def make_client_and_task():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    client = IndependentQAClient(
        publisher=FakePublisher(),
        workspace="/tmp/rusa-qa-client-test",
        service_url="http://qa:8010",
        service_token="qa-service-token-that-is-at-least-32-bytes-long",
        public_key_b64=base64.b64encode(public).decode("ascii"),
        key_id="qa-key-1",
        sandbox_image=SANDBOX_IMAGE,
    )
    task = CodingTask(
        objective="verify QA verdict",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/qa-test"),
        acceptance_criteria=["signed verdict required"],
        task_id="task-a",
        metadata={"tenant_id": "tenant-a"},
    )
    return private, client, task


def make_envelope(private, *, verdict="PASS", tenant_id="tenant-a", task_id="task-a", expires=None, artifact_digest=None):
    issued = datetime.now(timezone.utc)
    profiles = {
        profile: {
            "exit_code": 0 if verdict == "PASS" else (1 if profile == "pytest" else 0),
            "output_sha256": "e" * 64,
        }
        for profile in REQUIRED_QA_PROFILES
    }
    payload = {
        "schema_version": "rusa.qa-verdict.v1",
        "service_id": "rusa-independent-qa",
        "key_id": "qa-key-1",
        "tenant_id": tenant_id,
        "task_id": task_id,
        "git_tree_sha": "c" * 40,
        "artifact_digest": artifact_digest or digest_snapshot_archive(make_archive()),
        "sandbox_image": SANDBOX_IMAGE,
        "profiles": profiles,
        "verdict": verdict,
        "issued_at": issued.isoformat(),
        "expires_at": (expires or issued + timedelta(minutes=5)).isoformat(),
    }
    return sign_verdict(payload, private)


def test_worker_accepts_valid_signed_qa_pass():
    private, client, task = make_client_and_task()
    envelope = make_envelope(private)

    payload = client.verify_release_verdict(
        envelope,
        task,
        expected_tree_sha="c" * 40,
    )

    assert payload["verdict"] == "PASS"
    assert payload["git_tree_sha"] == "c" * 40


def test_worker_rejects_verdict_for_another_tenant_or_task():
    private, client, task = make_client_and_task()
    envelope = make_envelope(private, tenant_id="tenant-b")

    with pytest.raises(QAAttestationError, match="not bound"):
        client.verify_release_verdict(envelope, task)


def test_worker_rejects_expired_qa_verdict():
    private, client, task = make_client_and_task()
    envelope = make_envelope(
        private,
        expires=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    with pytest.raises(QAAttestationError, match="expired"):
        client.verify_release_verdict(envelope, task)


def test_worker_rejects_signed_fail_verdict_for_release():
    private, client, task = make_client_and_task()
    envelope = make_envelope(private, verdict="FAIL")

    with pytest.raises(QAAttestationError, match="did not issue a PASS"):
        client.verify_release_verdict(envelope, task)


def test_worker_rejects_tampered_signature():
    private, client, task = make_client_and_task()
    envelope = make_envelope(private)
    envelope["payload"]["git_tree_sha"] = "f" * 40

    with pytest.raises(QAAttestationError, match="signature"):
        client.verify_release_verdict(envelope, task)



def test_worker_rejects_signed_verdict_for_different_artifact_digest():
    private, client, task = make_client_and_task()
    envelope = make_envelope(private, artifact_digest="f" * 64)

    with pytest.raises(QAAttestationError, match="artifact digest"):
        client.verify_release_verdict(envelope, task)
