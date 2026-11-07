"""Immutable QA contract tests for safe snapshots and signed verdicts."""
import base64
import io
import tarfile
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
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


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def keys():
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public_bytes).decode("ascii")


def payload(*, verdict="PASS", tenant="tenant-a", task="task-a", expiry=None):
    issued = datetime.now(timezone.utc)
    return {
        "schema_version": "rusa.qa-verdict.v1",
        "service_id": "rusa-independent-qa",
        "key_id": "qa-key-1",
        "tenant_id": tenant,
        "task_id": task,
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
        "expires_at": (expiry or issued + timedelta(minutes=5)).isoformat(),
    }


def test_ed25519_verdict_is_bound_to_tenant_task_tree_and_runner():
    private, public_b64 = keys()
    from rusa_agents.qa_attestation import load_public_key

    envelope = sign_verdict(payload(), private)
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

    with pytest.raises(AttestationError):
        verify_verdict(
            envelope,
            load_public_key(public_b64),
            tenant_id="tenant-b",
            task_id="task-a",
            git_tree_sha="c" * 40,
            key_id="qa-key-1",
            sandbox_image=SANDBOX_IMAGE,
        )


def test_ed25519_verdict_rejects_signature_tampering_and_expiry():
    private, public_b64 = keys()
    from rusa_agents.qa_attestation import load_public_key

    public = load_public_key(public_b64)
    envelope = sign_verdict(payload(), private)
    envelope["payload"]["artifact_digest"] = "f" * 64
    with pytest.raises(AttestationError, match="signature"):
        verify_verdict(
            envelope, public, tenant_id="tenant-a", task_id="task-a",
            git_tree_sha="c" * 40, key_id="qa-key-1", sandbox_image=SANDBOX_IMAGE,
        )

    now = datetime.now(timezone.utc)
    expired = sign_verdict(
        payload(expiry=now - timedelta(seconds=1)),
        private,
    )
    with pytest.raises(AttestationError, match="expired"):
        verify_verdict(
            expired, public, tenant_id="tenant-a", task_id="task-a",
            git_tree_sha="c" * 40, key_id="qa-key-1", sandbox_image=SANDBOX_IMAGE,
        )


def test_signed_pass_cannot_contain_a_failed_profile():
    private, public_b64 = keys()
    from rusa_agents.qa_attestation import load_public_key

    body = payload()
    body["profiles"]["pytest"]["exit_code"] = 1
    envelope = sign_verdict(body, private)
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


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", ".env", "secrets/token.json"])
def test_snapshot_rejects_traversal_and_sensitive_paths(tmp_path, path):
    with pytest.raises(SnapshotError):
        extract_snapshot_archive(
            archive([(path, b"secret", 0o644)]),
            tmp_path / "snapshot",
        )


def test_snapshot_rejects_symlink_entries(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        item = tarfile.TarInfo("outside")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/passwd"
        tar.addfile(item)
    with pytest.raises(SnapshotError):
        extract_snapshot_archive(buffer.getvalue(), tmp_path / "snapshot")


def test_snapshot_digest_binds_content_and_executable_mode(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "script.py").write_bytes(b"same")
    (second / "script.py").write_bytes(b"same")
    (first / "script.py").chmod(0o644)
    (second / "script.py").chmod(0o755)

    assert digest_snapshot(first) != digest_snapshot(second)
    (second / "script.py").write_bytes(b"changed")
    assert digest_snapshot(first) != digest_snapshot(second)
