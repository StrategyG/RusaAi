from datetime import datetime, timedelta, timezone

import pytest

from rusa_agents.evidence import EvidenceAttestor


KEY = "enterprise-qa-test-master-key-" + "x" * 32


def attestor():
    return EvidenceAttestor(KEY)


def make_evidence(**overrides):
    values = {
        "profile": "pytest",
        "exit_code": 0,
        "image": "registry.test/rusa-sandbox@sha256:" + "a" * 64,
        "snapshot_sha256": "b" * 64,
        "task_id": "task-123",
        "tenant_id": "tenant-abc",
        "worker_role": "tester",
        "issued_at": datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc),
        "run_id": "00000000-0000-4000-8000-000000000001",
    }
    values.update(overrides)
    return attestor().attest(**values)


def verify(evidence, **overrides):
    expected = {
        "expected_profile": "pytest",
        "expected_worker_role": "tester",
        "expected_task_id": "task-123",
        "expected_tenant_id": "tenant-abc",
        "expected_image": "registry.test/rusa-sandbox@sha256:" + "a" * 64,
        "now": datetime(2026, 11, 7, 10, 1, tzinfo=timezone.utc),
    }
    expected.update(overrides)
    return attestor().verify(evidence, **expected)


def test_valid_attestation_is_bound_to_task_tenant_role_and_image():
    evidence = make_evidence()
    assert verify(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile", "bandit"),
        ("worker_role", "security"),
        ("task_id", "task-other"),
        ("tenant_id", "tenant-other"),
        ("image", "sandbox:latest"),
        ("snapshot_sha256", "c" * 64),
    ],
)
def test_attestation_rejects_scope_or_payload_tampering(field, value):
    evidence = make_evidence()
    evidence[field] = value
    assert not verify(evidence)


def test_attestation_rejects_signature_tampering():
    evidence = make_evidence()
    evidence["signature"] = "0" * 64
    assert not verify(evidence)


def test_attestation_rejects_expired_evidence():
    evidence = make_evidence()
    assert not verify(
        evidence,
        now=datetime(2026, 11, 7, 11, 1, tzinfo=timezone.utc),
    )


def test_attestation_rejects_future_timestamp():
    evidence = make_evidence(
        issued_at=datetime(2026, 11, 7, 10, 2, tzinfo=timezone.utc)
    )
    assert not verify(evidence)


def test_attestation_rejects_unexpected_fields():
    evidence = make_evidence()
    evidence["model_claim"] = "passed"
    assert not verify(evidence)


def test_attestation_requires_a_strong_master_key():
    with pytest.raises(ValueError, match="at least 32 bytes"):
        EvidenceAttestor("too-short")
