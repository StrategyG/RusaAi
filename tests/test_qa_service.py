"""Tests for the isolated QA service and authenticated validation endpoint."""
import asyncio
import base64
import io
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rusa_agents.qa_attestation import (
    REQUIRED_QA_PROFILES,
    load_public_key,
    verify_verdict,
)
from rusa_agents.qa_service import QAService, create_app
from rusa_agents.tool_gateway import ToolResult

SANDBOX_IMAGE = "registry.invalid/rusa-sandbox@sha256:" + "a" * 64
TOKEN = "qa-service-token-that-is-at-least-32-bytes-long"


class FakeSandbox:
    image = SANDBOX_IMAGE

    def __init__(self, failed_profile=None):
        self.failed_profile = failed_profile
        self.calls = []

    def run(self, workspace: Path, profile: str):
        self.calls.append(profile)
        assert (workspace / "README.md").read_text() == "RUSA-AI"
        code = 1 if profile == self.failed_profile else 0
        return ToolResult(
            ok=code == 0,
            output=f"{profile} output",
            evidence={"profile": profile, "exit_code": code, "image": self.image},
        )


def make_archive():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("README.md")
        info.size = len(b"RUSA-AI")
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(b"RUSA-AI"))
    return buffer.getvalue()


def service_for(tmp_path, failed_profile=None):
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    sandbox = FakeSandbox(failed_profile=failed_profile)
    service = QAService(
        token=TOKEN,
        private_key=private,
        key_id="qa-key-1",
        sandbox=sandbox,
        workspace_root=tmp_path,
    )
    return service, sandbox, base64.b64encode(public).decode("ascii")


def test_qa_service_runs_all_profiles_and_signs_snapshot(tmp_path):
    service, sandbox, public_b64 = service_for(tmp_path)
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
    assert sandbox.calls == list(REQUIRED_QA_PROFILES)


def test_qa_service_signs_failed_test_result_as_fail(tmp_path):
    service, _, public_b64 = service_for(tmp_path, failed_profile="ruff")
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


def test_qa_service_rejects_archive_larger_than_upload_limit(tmp_path):
    service, _, _ = service_for(tmp_path)
    service.max_upload_bytes = 10

    try:
        service.validate(
            tenant_id="tenant-a",
            task_id="task-a",
            git_tree_sha="c" * 40,
            archive_bytes=make_archive(),
        )
    except ValueError as exc:
        assert "upload limit" in str(exc)
    else:
        raise AssertionError("oversized archive was accepted")


def test_qa_http_endpoint_requires_service_token(tmp_path):
    service, _, _ = service_for(tmp_path)
    app = create_app(service)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://qa.test") as client:
            return await client.post(
                "/v1/validate",
                content=make_archive(),
                headers={
                    "Content-Type": "application/x-tar",
                    "X-Rusa-Tenant-ID": "tenant-a",
                    "X-Rusa-Task-ID": "task-a",
                    "X-Rusa-Git-Tree-SHA": "c" * 40,
                    "Authorization": "Bearer wrong-token",
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 401


def test_qa_http_endpoint_returns_signed_verdict_for_authenticated_worker(tmp_path):
    service, _, public_b64 = service_for(tmp_path)
    app = create_app(service)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://qa.test") as client:
            return await client.post(
                "/v1/validate",
                content=make_archive(),
                headers={
                    "Content-Type": "application/x-tar",
                    "X-Rusa-Tenant-ID": "tenant-a",
                    "X-Rusa-Task-ID": "task-a",
                    "X-Rusa-Git-Tree-SHA": "c" * 40,
                    "Authorization": f"Bearer {TOKEN}",
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    payload = verify_verdict(
        response.json(),
        load_public_key(public_b64),
        tenant_id="tenant-a",
        task_id="task-a",
        git_tree_sha="c" * 40,
        key_id="qa-key-1",
        sandbox_image=SANDBOX_IMAGE,
    )
    assert payload["verdict"] == "PASS"
