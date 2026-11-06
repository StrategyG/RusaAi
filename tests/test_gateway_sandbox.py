import asyncio
import base64
import json

import httpx
import pytest

from rusa_agents.contracts import RepositoryTarget
from rusa_agents.github_gateway import (
    ApprovalRequired,
    AuthorizationDenied,
    GitHubGateway,
)
from rusa_agents.policy import AutonomyPolicy
from rusa_agents.sandbox import DockerSandbox, SandboxError


def target():
    return RepositoryTarget("StrategyG/RusaAi", "main", "feature/safe-task")


def test_gateway_requires_explicit_repository_allowlist():
    with pytest.raises(ValueError):
        GitHubGateway("x" * 40, set())


def test_gateway_denies_repository_not_allowlisted():
    gateway = GitHubGateway("x" * 40, {"another/repo"})
    with pytest.raises(AuthorizationDenied):
        gateway._validate_target(target())


def test_gateway_denies_protected_files_without_approval():
    gateway = GitHubGateway("x" * 40, {"strategyg/rusaai"})
    with pytest.raises(ApprovalRequired):
        asyncio.run(
            gateway.write_file(
                target(),
                ".github/workflows/ci.yml",
                b"name: unsafe",
                "change workflow",
            )
        )


def test_gateway_rejects_path_traversal():
    with pytest.raises(ValueError):
        GitHubGateway._validate_path("../secrets.env")


def test_gateway_rejects_invalid_allowlist_repository():
    with pytest.raises(ValueError):
        GitHubGateway("x" * 40, {"owner/../repo"})


def test_gateway_never_reads_secret_bearing_paths():
    gateway = GitHubGateway("x" * 40, {"strategyg/rusaai"})
    with pytest.raises(AuthorizationDenied):
        asyncio.run(gateway.read_file(target(), ".env.production"))


def test_gateway_read_file_decodes_content():
    expected = b"print('safe')\n"

    def handler(request):
        assert request.method == "GET"
        assert request.url.params["ref"] == "feature/safe-task"
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(expected).decode(),
            },
        )

    gateway = GitHubGateway(
        "x" * 40,
        {"strategyg/rusaai"},
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(gateway.read_file(target(), "src/main.py")) == expected


def test_gateway_write_commits_only_to_work_branch():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        raw_path = request.url.raw_path.decode()
        if request.method == "GET" and "git/ref/heads/feature%2Fsafe-task" in raw_path:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        if request.method == "POST" and request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/feature/safe-task"})
        if request.method == "GET" and request.url.path.endswith("/contents/src/main.py"):
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "PUT" and request.url.path.endswith("/contents/src/main.py"):
            payload = json.loads(request.content)
            assert payload["branch"] == "feature/safe-task"
            assert payload["content"] == base64.b64encode(b"new file").decode()
            return httpx.Response(201, json={"commit": {"sha": "b" * 40}})
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    gateway = GitHubGateway(
        "x" * 40,
        {"strategyg/rusaai"},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        gateway.write_file(target(), "src/main.py", b"new file", "add source")
    )
    assert result["commit"]["sha"] == "b" * 40
    assert any(method == "PUT" for method, _ in calls)


def test_sandbox_requires_digest_pinned_image(tmp_path):
    with pytest.raises(ValueError):
        DockerSandbox(str(tmp_path), "python:3.11")


def test_sandbox_rejects_non_finite_limits(tmp_path):
    with pytest.raises(ValueError):
        DockerSandbox(
            str(tmp_path),
            "registry.example/runner@sha256:" + "a" * 64,
            cpu_limit=float("nan"),
        )


def test_sandbox_rejects_workspace_outside_root(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    sandbox = DockerSandbox(
        str(root),
        "registry.example/runner@sha256:" + "a" * 64,
    )
    with pytest.raises(SandboxError):
        sandbox._validate_workspace(str(outside))


def test_sandbox_command_is_networkless_and_uses_gvisor(tmp_path):
    root = tmp_path / "root"
    workspace = root / "task"
    workspace.mkdir(parents=True)
    sandbox = DockerSandbox(
        str(root),
        "registry.example/runner@sha256:" + "a" * 64,
    )
    argv = sandbox._build_command(workspace, ["python", "-m", "pytest"])
    assert "--network=none" in argv
    assert "--runtime=runsc" in argv
    assert "--cap-drop=ALL" in argv
    assert "--pull=never" in argv
    assert "python" in argv
    assert "pytest" in argv


def test_sandbox_rejects_docker_without_rootless_and_gvisor():
    with pytest.raises(SandboxError):
        DockerSandbox._validate_runtime_info(
            '["name=seccomp,profile=builtin"]',
            '{"runc":{"path":"runc"}}',
        )
    DockerSandbox._validate_runtime_info(
        '["name=seccomp,profile=builtin","name=rootless"]',
        '{"runc":{"path":"runc"},"runsc":{"path":"runsc"}}',
    )


def test_policy_invalid_risk_fails_closed():
    decision = AutonomyPolicy().evaluate("repo_read", "critical")
    assert not decision.allowed
