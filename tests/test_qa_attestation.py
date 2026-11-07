"""Unit tests for safe artifact snapshots and Ed25519 QA verdicts."""
import base64
import io
import stat
import tarfile
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rusa_agents.qa_attestation import (
    REQUIRED_QA_PROFILES,
    AttestationError,
    SnapshotError,
    digest_snapshot,
    extract_snapshot_archive,
    sign_verdict,
    verify_verdict,
)

SANDBOX_IMAGE = "registry.invalid/rusa-sandbox@sha256:" + "a" * 64


def make_archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = mode
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def key_pair():
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        private,
        base64.b64encode(private_raw).decode("ascii"),
        base64.b64encode(public_raw).decode("ascii"),
    )


def verdict_payload(*, verdict="PASS", issued_at=None, expires_at=None):
    issued = issued_at or datetime.now(timezone.utc)
    expires = expires_at or issued + timedelta(minutes=5)
    return {
        "schema_version": "rusa.qa-verdict.v1",
        "service_id": "rusa-independent-qa",
        "key_id": "qa-key-1",
        "tenant_id": "tenant-a",
        "task_id": "task-a",
        "git_tree_sha": "c" * 40,
        "artifact_digest": "d" * 64,
        "sandbox_image": SANDBOX_IMAGE,
        "profiles": {
            profile: {
                "exit_code": 0 if verdict == "PASS" else (1 if profile == "pytest" else 0),
                "output_sha256": "e" * 64,
            }
            for profile in REQUIRED_QA_PROFILES
        },
        "verdict": verdict,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
    }


def test_snapshot_digest_is_stable_after_read_only_extraction(tmp_path):
    archive = make_archive([
        ("README.md", b"RUSA-AI QA snapshot", 0o644),
        ("scripts/check.py", b"print('ok')", 0o755),
    ])
    root = tmp_path / "snapshot"
    digest = extract_snapshot_archive(archive, root)

    assert digest == digest_snapshot(root)
    assert (root / "README.md").read_bytes() == b"RUSA-AI QA snapshot"
    assert stat.S_IMODE((root / "README.md").stat().st_mode) == 0o444
    assert stat.S_IMODE((root / "scripts/check.py").stat().st_mode) == 0o555


def test_snapshot_digest_binds_file_content_and_executable_mode(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "run.sh").write_bytes(b"same")
    (second / "run.sh").write_bytes(b"same")
    (first / "run.sh").chmod(0o644)
    (second / "run.sh").chmod(0o755)

    assert digest_snapshot(first) != digest_snapshot(second)
    (second / "run.sh").write_bytes(b"changed")
    assert digest_snapshot(first) != digest_snapshot(second)


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", ".env", "secrets/token.json"])
def test_snapshot_rejects_traversal_and_sensitive_paths(tmp_path, path):
    archive = make_archive([(path, b"blocked", 0o644)])

    with pytest.raises(SnapshotError):
        extract_snapshot_archive(archive, tmp_path / "snapshot")


def test_snapshot_rejects_duplicate_paths(tmp_path):
    archive = make_archive([
        ("README.md", b"first", 0o644),
        ("README.md", b"second", 0o644),
    ])

    with pytest.raises(SnapshotError, match="duplicate"):
        extract_snapshot_archive(archive, tmp_path / "snapshot")


def test_snapshot_rejects_symbolic_links(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        link = tarfile.TarInfo("outside")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

    with pytest.raises(SnapshotError, match="regular files"):
        extract_snapshot_archive(buffer.getvalue(), tmp_path / "snapshot")


def test_snapshot_enforces_file_and_total_size_limits(tmp_path):
    archive = make_archive([("large.bin", b"x" * 20, 0o644)])

    with pytest.raises(SnapshotError, match="file exceeds"):
        extract_snapshot_archive(
            archive,
            tmp_path / "snapshot-file-limit",
            max_file_bytes=10,
        )
    with pytest.raises(SnapshotError, match="total size"):
        extract_snapshot_archive(
            archive,
            tmp_path / "snapshot-total-limit",
            max_total_bytes=10,
        )


def test_ed25519_verdict_is_bound_to_tenant_task_and_git_tree():
    private, _, public_b64 = key_pair()
    from rusa_agents.qa_attestation import load_public_key

    envelope = sign_verdict(verdict_payload(), private)
    verified = verify_verdict(
        envelope,
        load_public_key(public_b64),
        tenant_id="tenant-a",
        task_id="task-a",
        git_tree_sha="c" * 40,
        key_id="qa-key-1",
        sandbox_image=SANDBOX_IMAGE,
    )

    assert verified["verdict"] == "PASS"
    assert verified["artifact_digest"] == "d" * 64

    with pytest.raises(AttestationError, match="not bound"):
        verify_verdict(
            envelope,
            load_public_key(public_b64),
            tenant_id="tenant-b",
            task_id="task-a",
            git_tree_sha="c" * 40,
            key_id="qa-key-1",
            sandbox_image=SANDBOX_IMAGE,
        )


def test_ed25519_verdict_rejects_tampering_and_expired_evidence():
    private, _, public_b64 = key_pair()
    from rusa_agents.qa_attestation import load_public_key

    public = load_public_key(public_b64)
    envelope = sign_verdict(verdict_payload(), private)
    envelope["payload"]["artifact_digest"] = "f" * 64
    with pytest.raises(AttestationError, match="signature"):
        verify_verdict(
            envelope,
            public,
            tenant_id="tenant-a",
            task_id="task-a",
            git_tree_sha="c" * 40,
            key_id="qa-key-1",
            sandbox_image=SANDBOX_IMAGE,
        )

    now = datetime.now(timezone.utc)
    expired = sign_verdict(
        verdict_payload(
            issued_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(minutes=1),
        ),
        private,
    )
    with pytest.raises(AttestationError, match="expired"):
        verify_verdict(
            expired,
            public,
            tenant_id="tenant-a",
            task_id="task-a",
            git_tree_sha="c" * 40,
            key_id="qa-key-1",
            sandbox_image=SANDBOX_IMAGE,
        )


def test_signed_verdict_cannot_claim_pass_when_a_profile_failed():
    private, _, public_b64 = key_pair()
    from rusa_agents.qa_attestation import load_public_key

    payload = verdict_payload(verdict="PASS")
    payload["profiles"]["pytest"]["exit_code"] = 1
    envelope = sign_verdict(payload, private)

    with pytest.raises(AttestationError, match="does not match"):
        verify_verdict(
            envelope,
            load_public_key(public_b64),
            tenant_id="tenant-a",
            task_id="task-a",
            git_tree_sha="c" * 40,
            key_id="qa-key-1",
            sandbox_image=SANDBOX_IMAGE,
        )
