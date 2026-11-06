import os
import subprocess
from pathlib import Path

import pytest

from rusa_agents.contracts import WorkerRole
from rusa_agents.tool_gateway import DockerSandbox, ToolDenied, WorkspaceToolGateway


def test_gateway_rejects_path_traversal(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.read_file("../outside.txt")


def test_gateway_rejects_secret_paths(tmp_path):
    (tmp_path / ".env").write_text("DO_NOT_READ")
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.read_file(".env")


def test_only_developer_can_write(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.write_file("src/example.py", "print('x')", WorkerRole.ANALYST)


def test_developer_can_write_inside_workspace(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    result = gateway.write_file("src/example.py", "print('ok')", WorkerRole.DEVELOPER)
    assert result.ok
    assert (tmp_path / "src/example.py").read_text() == "print('ok')"


def test_security_worker_cannot_run_pytest(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.run_check("pytest", WorkerRole.SECURITY)



def test_sandbox_uses_snapshot_without_git_or_secret_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src.py").write_text("print('safe')")
    (workspace / ".env").write_text("SECRET=never-copy")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("credential=private")
    (workspace / "private.pem").write_text("PRIVATE KEY")
    (workspace / "id_rsa").write_text("PRIVATE KEY")
    (workspace / ".npmrc").write_text("//registry.invalid/:_authToken=secret")
    (workspace / "terraform.tfstate").write_text("state-secret")
    secret_target = workspace / "outside-secret.txt"
    secret_target.write_text("SECRET")
    (workspace / "linked-secret.txt").symlink_to(secret_target)

    captured_command = []
    chown_calls = []
    monkeypatch.setenv("RUSA_DOCKER_GID", "1001")
    monkeypatch.setattr(
        "rusa_agents.tool_gateway.os.chown",
        lambda path, uid, gid: chown_calls.append((path, uid, gid)),
    )

    def fake_bounded(self, command, timeout_seconds, env, max_output_bytes):
        captured_command.extend(command)
        mount = next(item for item in command if item.startswith("type=bind,src="))
        snapshot = mount.split("src=", 1)[1].split(",dst=", 1)[0]
        snapshot_path = Path(snapshot)
        assert mount.endswith(",readonly")
        assert snapshot_path.stat().st_mode & 0o777 == 0o555
        assert snapshot_path.parent.stat().st_mode & 0o777 == 0o750
        assert (snapshot_path / "src.py").stat().st_mode & 0o777 == 0o444
        assert (snapshot_path / "src.py").is_file()
        assert not (snapshot_path / ".env").exists()
        assert not (snapshot_path / ".git").exists()
        assert not (snapshot_path / "private.pem").exists()
        assert not (snapshot_path / "id_rsa").exists()
        assert not (snapshot_path / ".npmrc").exists()
        assert not (snapshot_path / "terraform.tfstate").exists()
        assert not (snapshot_path / "linked-secret.txt").exists()
        return 0, "passed", "completed", 6

    monkeypatch.setattr("rusa_agents.tool_gateway.DOCKER_BIN", "/usr/bin/docker")
    monkeypatch.setattr(DockerSandbox, "_run_bounded", fake_bounded)
    result = DockerSandbox(image="sandbox:test").run(workspace, "pytest")
    assert result.ok
    assert result.evidence["profile"] == "pytest"
    assert chown_calls and chown_calls[0][2] == 1001
    assert "PYTHONPYCACHEPREFIX=/run/rusa-tmp/pycache" in captured_command


def test_gateway_rejects_credential_like_files(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.read_file("private.pem")
    with pytest.raises(ToolDenied):
        gateway.write_file("credentials.json", "{}", WorkerRole.DEVELOPER)


def test_gateway_rejects_symlink_paths(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    link = tmp_path / "linked.txt"
    link.symlink_to(secret)
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.read_file("linked.txt")


def test_gateway_rejects_unknown_tool_arguments(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.execute(
            {"name": "list_files", "arguments": {"path": ".", "shell": "id"}},
            WorkerRole.ANALYST,
        )


def test_gateway_atomic_write_creates_expected_content(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    result = gateway.write_file("src/generated.py", "VALUE = 42\n", WorkerRole.DEVELOPER)
    assert result.ok
    assert (tmp_path / "src" / "generated.py").read_text() == "VALUE = 42\n"



def test_tester_worker_cannot_run_security_profile(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.run_check("bandit", WorkerRole.TESTER)


def test_case_insensitive_workflow_path_is_protected(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.read_file(".GITHUB/workflows/ci.yml")


def test_case_insensitive_migration_write_is_protected(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.write_file("MIGRATIONS/unsafe.sql", "DROP TABLE users;", WorkerRole.DEVELOPER)


@pytest.mark.parametrize(
    "path",
    ["Dockerfile.worker", "docker-compose.override.yml", "compose.yaml",
     "requirements-dev.txt", "infra/prod.tf", "K8S/deployment.yaml"],
)
def test_protected_deployment_paths(tmp_path, path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.write_file(path, "x", WorkerRole.DEVELOPER)



@pytest.mark.parametrize(
    "name",
    [".env.production", ".envrc", ".envrc.local", "config.env", "app.env.local", "production.env", "secrets.yaml"],
)
def test_gateway_blocks_env_suffix_files(tmp_path, name):
    (tmp_path / name).write_text("SENSITIVE=value")
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.read_file(name)
    with pytest.raises(ToolDenied):
        gateway.write_file(name, "new value", WorkerRole.DEVELOPER)


def test_sandbox_snapshot_excludes_non_hidden_env_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('safe')")
    (workspace / "production.env").write_text("SECRET=do-not-copy")
    (workspace / "config.env.local").write_text("TOKEN=do-not-copy")
    (workspace / ".envrc").write_text("TOKEN=do-not-copy")
    (workspace / ".direnv").mkdir()
    (workspace / ".direnv" / "secrets").write_text("TOKEN=do-not-copy")
    (workspace / "secrets.yaml").write_text("password: do-not-copy")

    def fake_bounded(self, command, timeout_seconds, env, max_output_bytes):
        mount = next(item for item in command if item.startswith("type=bind,src="))
        snapshot = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
        assert not (snapshot / "production.env").exists()
        assert not (snapshot / "config.env.local").exists()
        assert not (snapshot / ".envrc").exists()
        assert not (snapshot / ".direnv").exists()
        assert not (snapshot / "secrets.yaml").exists()
        return 0, "passed", "completed", 6

    monkeypatch.setattr("rusa_agents.tool_gateway.DOCKER_BIN", "/usr/bin/docker")
    monkeypatch.setattr(DockerSandbox, "_run_bounded", fake_bounded)
    assert DockerSandbox(image="sandbox:test").run(workspace, "pytest").ok



def test_gateway_rejects_secret_manifest_names(tmp_path):
    (tmp_path / "secrets.yaml").write_text("token: should-not-be-read")
    (tmp_path / "credentials.toml").write_text("password = 'hidden'")
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.read_file("secrets.yaml")
    with pytest.raises(ToolDenied):
        gateway.read_file("credentials.toml")


def test_sandbox_snapshot_enforces_file_count_limit(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.py").write_text("print('one')")
    (workspace / "two.py").write_text("print('two')")
    sandbox = DockerSandbox(max_snapshot_files=1)
    result = sandbox.run(workspace, "pytest")
    assert result.ok is False
    assert "file-count limit" in result.output



def test_sandbox_bounded_runner_limits_output():
    import sys

    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 1000000); sys.stdout.flush()",
    ]
    code, output, outcome, output_bytes = DockerSandbox._run_bounded(
        command,
        timeout_seconds=10,
        env=os.environ.copy(),
        max_output_bytes=4096,
    )
    assert outcome == "output_limit"
    assert len(output.encode("utf-8")) <= 4096
    assert output_bytes > 4096
    assert code is not None
