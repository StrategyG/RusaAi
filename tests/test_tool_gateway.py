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
    (workspace / ".env.local").write_text("SECRET=never-copy")
    (workspace / ".env.production").write_text("SECRET=never-copy")
    (workspace / ".env.example").write_text("SAFE_TEMPLATE=example")
    (workspace / ".env.cpu.example").write_text("SAFE_TEMPLATE=cpu")
    (workspace / ".env.cpu-8gb.example").write_text("SAFE_TEMPLATE=cpu-8gb")
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

    def fake_run(command, **kwargs):
        captured_command.extend(command)
        mount = next(item for item in command if item.startswith("type=bind,src="))
        snapshot = mount.split("src=", 1)[1].split(",dst=", 1)[0]
        snapshot_path = Path(snapshot)
        assert mount.endswith(",readonly")
        assert snapshot_path.stat().st_mode & 0o777 == 0o555
        assert (snapshot_path / "src.py").stat().st_mode & 0o777 == 0o444
        assert (snapshot_path / "src.py").is_file()
        assert not (snapshot_path / ".env").exists()
        assert not (snapshot_path / ".env.local").exists()
        assert not (snapshot_path / ".env.production").exists()
        assert (snapshot_path / ".env.example").read_text() == "SAFE_TEMPLATE=example"
        assert (snapshot_path / ".env.cpu.example").read_text() == "SAFE_TEMPLATE=cpu"
        assert (snapshot_path / ".env.cpu-8gb.example").read_text() == "SAFE_TEMPLATE=cpu-8gb"
        assert not (snapshot_path / ".git").exists()
        assert not (snapshot_path / "private.pem").exists()
        assert not (snapshot_path / "id_rsa").exists()
        assert not (snapshot_path / ".npmrc").exists()
        assert not (snapshot_path / "terraform.tfstate").exists()
        assert not (snapshot_path / "linked-secret.txt").exists()
        return subprocess.CompletedProcess(command, 0, stdout="passed", stderr="")

    monkeypatch.setattr("rusa_agents.tool_gateway.DOCKER_BIN", "/usr/bin/docker")
    monkeypatch.setattr("rusa_agents.tool_gateway.subprocess.run", fake_run)
    result = DockerSandbox(image="sandbox:test").run(workspace, "pytest")
    assert result.ok
    assert result.evidence["profile"] == "pytest"
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


def test_external_rag_tool_is_tenant_and_project_scoped(tmp_path):
    import json

    from rusa_agents.external_rag import ExternalRAGEvidence

    class FakeExternalRAG:
        def __init__(self):
            self.calls = []

        async def search(self, query, *, top_k, filters):
            self.calls.append((query, top_k, filters))
            return [
                ExternalRAGEvidence(
                    evidence_id="evidence-1",
                    text="Untrusted retrieved evidence.",
                    source_uri="https://docs.example.org/guide",
                    title="Guide",
                    revision="rev-7",
                    content_sha256="a" * 64,
                    locator="section:2",
                    score=0.81,
                    metadata={"language": "en"},
                )
            ]

    client = FakeExternalRAG()
    gateway = WorkspaceToolGateway(
        tmp_path,
        external_rag_client=client,
        external_rag_tenant_id="tenant-a",
        knowledge_project_id="strategy--rusaai",
    )
    result = gateway.execute(
        {"name": "search_external_rag", "arguments": {"query": "find the design", "top_k": 2}},
        WorkerRole.ANALYST,
    )
    payload = json.loads(result.output)
    assert result.ok
    assert client.calls == [
        ("find the design", 2, {"tenant_id": "tenant-a", "project_id": "strategy--rusaai"})
    ]
    assert payload["results"][0]["trust"] == "untrusted_external_evidence"
    assert payload["results"][0]["source"]["revision"] == "rev-7"
    assert payload["results"][0]["source"]["content_sha256"] == "a" * 64


def test_external_rag_tool_fails_closed_when_not_configured(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied, match="not enabled"):
        gateway.execute(
            {"name": "search_external_rag", "arguments": {"query": "find evidence"}},
            WorkerRole.ANALYST,
        )


def test_external_rag_tool_rejects_unbounded_or_invalid_arguments(tmp_path):
    class FakeExternalRAG:
        async def search(self, query, *, top_k, filters):
            raise AssertionError("invalid arguments must be rejected before provider access")

    gateway = WorkspaceToolGateway(
        tmp_path,
        external_rag_client=FakeExternalRAG(),
        external_rag_tenant_id="tenant-a",
    )
    with pytest.raises(ToolDenied, match="top_k"):
        gateway.execute(
            {"name": "search_external_rag", "arguments": {"query": "x", "top_k": 100}},
            WorkerRole.ANALYST,
        )


def test_external_rag_outage_returns_local_fallback_instead_of_blocking(tmp_path):
    from rusa_agents.external_rag import ExternalRAGError

    class OfflineExternalRAG:
        async def search(self, query, *, top_k, filters):
            raise ExternalRAGError("External RAG provider is unavailable or returned invalid JSON")

    gateway = WorkspaceToolGateway(
        tmp_path,
        external_rag_client=OfflineExternalRAG(),
        external_rag_tenant_id="tenant-a",
    )
    result = gateway.execute(
        {"name": "search_external_rag", "arguments": {"query": "find evidence"}},
        WorkerRole.ANALYST,
    )
    assert not result.ok
    assert "External RAG is unavailable" in result.output
    assert result.evidence["available"] is False


def test_mcp_tools_are_tenant_scoped_allowlisted_and_marked_untrusted(tmp_path):
    import json

    class FakeMCPClient:
        allowed_tools = frozenset({"search_docs"})

        async def list_tools(self):
            return [
                {
                    "name": "search_docs",
                    "description": "Read-only document search",
                    "input_schema": {"type": "object"},
                }
            ]

        async def call_tool(self, name, arguments):
            assert name == "search_docs"
            assert arguments == {"query": "RusaAi"}
            return {"matches": ["doc-1"]}

    client = FakeMCPClient()
    gateway = WorkspaceToolGateway(
        tmp_path,
        mcp_client=client,
        mcp_tenant_id="tenant-a",
    )
    listed = gateway.execute(
        {"name": "list_mcp_tools", "arguments": {}},
        WorkerRole.ANALYST,
    )
    listed_payload = json.loads(listed.output)
    assert listed.ok
    assert listed_payload["trust"] == "untrusted_remote_mcp_metadata"
    assert listed_payload["tools"][0]["name"] == "search_docs"
    assert listed.evidence["tenant_id"] == "tenant-a"

    called = gateway.execute(
        {
            "name": "call_mcp_tool",
            "arguments": {"tool_name": "search_docs", "arguments": {"query": "RusaAi"}},
        },
        WorkerRole.ANALYST,
    )
    called_payload = json.loads(called.output)
    assert called.ok
    assert called_payload["trust"] == "untrusted_remote_mcp_result"
    assert called_payload["result"]["matches"] == ["doc-1"]


def test_mcp_tools_fail_closed_when_no_tenant_connection_is_configured(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied, match="not enabled"):
        gateway.execute(
            {"name": "list_mcp_tools", "arguments": {}},
            WorkerRole.ANALYST,
        )


def test_mcp_provider_outage_returns_nonfatal_local_fallback(tmp_path):
    class OfflineMCPClient:
        async def list_tools(self):
            raise RuntimeError("network is offline")

    gateway = WorkspaceToolGateway(
        tmp_path,
        mcp_client=OfflineMCPClient(),
        mcp_tenant_id="tenant-a",
    )
    result = gateway.execute(
        {"name": "list_mcp_tools", "arguments": {}},
        WorkerRole.ANALYST,
    )
    assert not result.ok
    assert "continue with local tools" in result.output
    assert result.evidence["available"] is False
