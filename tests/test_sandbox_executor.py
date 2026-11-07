import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rusa_agents.sandbox_executor import (
    SandboxRunRequest,
    _validate_workspace,
    _workspace_for_lease,
    create_app,
)
from rusa_agents.tool_gateway import ToolDenied, ToolResult


class FakeVerifier:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    def verify_ready(self):
        return None

    def verify_lease(self, task_id, tenant_id, worker_id, lease_token):
        self.calls.append((task_id, tenant_id, worker_id, lease_token))
        return self.allowed


class FakeSandbox:
    image = "sandbox:test"
    rootless = True

    def __init__(self):
        self.calls = []

    def verify_ready(self, require_rootless=False, require_image=False):
        if require_rootless and not self.rootless:
            raise RuntimeError("not rootless")

    def run(self, workspace, profile):
        self.calls.append((workspace, profile))
        return ToolResult(True, "pytest passed", {"profile": profile, "exit_code": 0})


def _request(tenant_id, task_id, worker_id, lease_token, profile="pytest"):
    return {
        "tenant_id": tenant_id,
        "task_id": str(task_id),
        "worker_id": worker_id,
        "lease_token": str(lease_token),
        "profile": profile,
    }


def test_request_contract_rejects_arbitrary_paths_and_commands():
    payload = _request("tenant-a", uuid4(), "worker-a", uuid4())
    with pytest.raises(ValueError):
        SandboxRunRequest.model_validate({**payload, "workspace": "/etc"})
    with pytest.raises(ValueError):
        SandboxRunRequest.model_validate({**payload, "command": ["id"]})


def test_workspace_path_is_deterministic_and_tenant_scoped(tmp_path):
    task_id, lease_token = uuid4(), uuid4()
    first = _workspace_for_lease(tmp_path, "tenant-a", task_id, lease_token)
    second = _workspace_for_lease(tmp_path, "tenant-b", task_id, lease_token)
    assert first != second
    assert first == (
        tmp_path
        / hashlib.sha256(b"tenant-a").hexdigest()[:20]
        / str(task_id)
        / hashlib.sha256(str(lease_token).encode()).hexdigest()[:12]
    )


def test_workspace_validation_rejects_symlinked_tenant_directory(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    task_id, lease_token = uuid4(), uuid4()
    tenant_key = hashlib.sha256(b"tenant-a").hexdigest()[:20]
    (root / tenant_key).symlink_to(outside, target_is_directory=True)
    candidate = _workspace_for_lease(root, "tenant-a", task_id, lease_token)
    with pytest.raises((OSError, ToolDenied)):
        _validate_workspace(root, candidate)


def test_executor_requires_bearer_token_and_active_lease(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("RUSA_SANDBOX_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    verifier = FakeVerifier(allowed=False)
    sandbox = FakeSandbox()
    secret = "s" * 48
    app = create_app(
        sandbox=sandbox,
        verifier=verifier,
        workspace_root=root,
        token=secret,
        require_rootless=False,
        require_image=False,
        max_concurrency=1,
    )
    tenant_id, task_id, lease_token = "tenant-a", uuid4(), uuid4()
    workspace = _workspace_for_lease(root, tenant_id, task_id, lease_token)
    workspace.mkdir(parents=True)
    payload = _request(tenant_id, task_id, "worker-a", lease_token)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/v1/run", json=payload).status_code == 401
        denied = client.post(
            "/v1/run",
            json=payload,
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert denied.status_code == 409
    assert sandbox.calls == []


def test_executor_runs_only_the_active_leased_workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("RUSA_SANDBOX_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    verifier = FakeVerifier(allowed=True)
    sandbox = FakeSandbox()
    secret = "t" * 48
    app = create_app(
        sandbox=sandbox,
        verifier=verifier,
        workspace_root=root,
        token=secret,
        require_rootless=False,
        require_image=False,
        max_concurrency=1,
    )
    tenant_id, task_id, lease_token = "tenant-a", uuid4(), uuid4()
    workspace = _workspace_for_lease(root, tenant_id, task_id, lease_token)
    workspace.mkdir(parents=True)
    payload = _request(tenant_id, task_id, "worker-a", lease_token, "ruff")

    with TestClient(app) as client:
        response = client.post(
            "/v1/run",
            json=payload,
            headers={"Authorization": f"Bearer {secret}"},
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert sandbox.calls == [(workspace, "ruff")]
    assert verifier.calls == [(task_id, tenant_id, "worker-a", lease_token)]


def test_executor_rejects_unknown_profile_and_extra_request_fields(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("RUSA_SANDBOX_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    app = create_app(
        sandbox=FakeSandbox(),
        verifier=FakeVerifier(),
        workspace_root=root,
        token="u" * 48,
        require_rootless=False,
        require_image=False,
    )
    payload = _request("tenant-a", uuid4(), "worker-a", uuid4(), "shell")
    with TestClient(app) as client:
        bad_profile = client.post(
            "/v1/run",
            json=payload,
            headers={"Authorization": f"Bearer {'u' * 48}"},
        )
        extra_field = client.post(
            "/v1/run",
            json={**_request("tenant-a", uuid4(), "worker-a", uuid4()), "command": ["id"]},
            headers={"Authorization": f"Bearer {'u' * 48}"},
        )
    assert bad_profile.status_code == 422
    assert extra_field.status_code == 422
