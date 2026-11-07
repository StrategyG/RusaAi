"""Immutable QA contract tests for the isolated signing service."""
import base64
import io
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rusa_agents.qa_attestation import (
    REQUIRED_QA_PROFILES,
    load_public_key,
    verify_verdict,
)
from rusa_agents.qa_service import QAService
from rusa_agents.tool_gateway import ToolResult

SANDBOX_IMAGE = "registry.invalid/rusa-sandbox@sha256:" + "a" * 64


class Sandbox:
    image = SANDBOX_IMAGE

    def __init__(self, fail=None):
        self.fail = fail
        self.profiles = []

    def run(self, workspace: Path, profile: str):
        self.profiles.append(profile)
        assert (workspace / "README.md").read_text() == "candidate"
        code = 1 if profile == self.fail else 0
        return ToolResult(
            ok=code == 0,
            output=f"{profile} completed",
            evidence={"profile": profile, "exit_code": code, "image": self.image},
        )


def make_archive():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        item = tarfile.TarInfo("README.md")
        item.size = len(b"candidate")
        item.mode = 0o644
        tar.addfile(item, io.BytesIO(b"candidate"))
    return buffer.getvalue()


def make_service(tmp_path, failed=None):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    sandbox = Sandbox(fail=failed)
    service = QAService(
        token="qa-service-token-that-is-longer-than-thirty-two-bytes",
        private_key=private,
        key_id="qa-key-1",
        sandbox=sandbox,
        workspace_root=tmp_path,
    )
    return service, sandbox, base64.b64encode(public).decode("ascii")


def test_qa_service_runs_fixed_profiles_and_signs_the_snapshot(tmp_path):
    service, sandbox, public_b64 = make_service(tmp_path)
    envelope = service.validate(
        tenant_id="tenant-a",
        task_id="task-a",
        git_tree_sha="c" * 40,
        archive_bytes=make_archive(),
    )
    payload = verify_verdict(
        envelope,
        load_public_key(public_b64),
        tenant_id="tenant-a",
        task_id="task-a",
        git_tree_sha="c" * 40,
        key_id="qa-key-1",
        sandbox_image=SANDBOX_IMAGE,
    )
    assert payload["verdict"] == "PASS"
    assert set(payload["profiles"]) == set(REQUIRED_QA_PROFILES)
    assert sandbox.profiles == list(REQUIRED_QA_PROFILES)


def test_qa_service_signs_nonzero_profile_as_fail(tmp_path):
    service, _, public_b64 = make_service(tmp_path, failed="ruff")
    envelope = service.validate(
        tenant_id="tenant-a",
        task_id="task-a",
        git_tree_sha="c" * 40,
        archive_bytes=make_archive(),
    )
    payload = verify_verdict(
        envelope,
        load_public_key(public_b64),
        tenant_id="tenant-a",
        task_id="task-a",
        git_tree_sha="c" * 40,
        key_id="qa-key-1",
        sandbox_image=SANDBOX_IMAGE,
    )
    assert payload["verdict"] == "FAIL"
    assert payload["profiles"]["ruff"]["exit_code"] == 1
