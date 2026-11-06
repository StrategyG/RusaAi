import asyncio
import json

import pytest

from rusa_agents.audit import AuditLog
from rusa_agents.contracts import (
    CodingTask,
    RepositoryTarget,
    TaskRecord,
    TaskState,
    ToolEvidence,
    WorkerResult,
    WorkerRole,
)
from rusa_agents.github_gateway import GitHubAPIError, GitHubRepositoryGateway, GitHubToolGateway
from rusa_agents.memory import SQLiteMemory
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.policy import AutonomyPolicy, RiskLevel
from rusa_agents.providers import OpenAICompatibleProvider
from rusa_agents.runtime import build_orchestrator
from rusa_agents.worker import run_worker
from rusa_agents.workers import AgentWorker


def make_task():
    return CodingTask(
        objective="Implement the agent runtime",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/aicode-test"),
        acceptance_criteria=["Tests pass", "Security checks pass"],
    )


def test_policy_rejects_protected_and_prohibited_actions():
    policy = AutonomyPolicy()
    assert policy.evaluate("repo.file.write").allowed
    assert policy.evaluate("repo.merge").requires_approval
    assert not policy.evaluate("audit.disable").allowed
    assert policy.evaluate("repo.read", RiskLevel.CRITICAL).requires_approval


def test_empty_allowlist_does_not_restore_defaults():
    policy = AutonomyPolicy(set())
    assert not policy.evaluate("repo.read").allowed


def test_repository_target_rejects_same_branch_and_traversal():
    with pytest.raises(ValueError):
        RepositoryTarget("owner/repo", "main", "main")
    with pytest.raises(ValueError):
        RepositoryTarget("owner/repo", "main", "feature/../main")


def test_task_state_machine_rejects_invalid_transition():
    record = TaskRecord(make_task())
    with pytest.raises(ValueError):
        record.transition(TaskState.SUCCEEDED)


def test_memory_is_tenant_scoped(tmp_path):
    memory = SQLiteMemory(str(tmp_path / "memory.sqlite3"))
    memory.save("task-1", "owner/repo", "executing", {"step": 1}, tenant_id="tenant-a")
    assert memory.get("task-1", tenant_id="tenant-a")["payload"]["step"] == 1
    assert memory.get("task-1", tenant_id="tenant-b") is None


def test_audit_log_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(str(path))
    audit.record("task.started", "task-1", "master")
    audit.record("task.finished", "task-1", "master")
    assert audit.verify()
    entries = path.read_text(encoding="utf-8").splitlines()
    changed = json.loads(entries[0])
    changed["actor"] = "attacker"
    entries[0] = json.dumps(changed)
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    assert not audit.verify()


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    async def complete(self, *, system_prompt, user_prompt):
        return json.dumps(self.responses.pop(0))


class FakeTools:
    ROLE_ACTIONS = {WorkerRole.DEVELOPER: {"repo.create_branch"}}

    def __init__(self):
        self.calls = []

    async def execute(self, action, arguments, task, role):
        self.calls.append((action, arguments, task.task_id, role))
        return {"ok": True, "data": {"created": True}, "evidence": [], "artifacts": []}


def test_worker_executes_only_explicit_tool_calls():
    provider = FakeProvider(
        [
            {
                "summary": "Create the isolated branch.",
                "final": False,
                "success": False,
                "tool_calls": [{"action": "repo.create_branch", "arguments": {}}],
                "blockers": [],
            },
            {
                "summary": "Branch created.",
                "final": True,
                "success": True,
                "tool_calls": [],
                "blockers": [],
            },
        ]
    )
    tools = FakeTools()
    worker = AgentWorker(WorkerRole.DEVELOPER, provider, tools)
    result = asyncio.run(worker.run(make_task(), []))
    assert result.success
    assert len(tools.calls) == 1


class StubWorker:
    def __init__(self, role, evidence=None):
        self.role = role
        self.evidence = evidence or []

    async def run(self, task, prior_results):
        return WorkerResult(
            role=self.role,
            summary="completed",
            success=True,
            evidence=list(self.evidence),
        )


VALID_COMMIT_SHA = "a" * 40
CI_SOURCE = "https://github.com/StrategyG/RusaAi/runs/123456"


def make_workers(with_evidence=True, security_sha=VALID_COMMIT_SHA, quality_source=CI_SOURCE):
    pull_request = (
        [
            ToolEvidence(
                "pull_request",
                "draft_pull_request",
                "draft",
                "https://github.com/StrategyG/RusaAi/pull/123",
                VALID_COMMIT_SHA,
            )
        ]
        if with_evidence
        else []
    )
    quality = (
        [
            ToolEvidence(
                "ci_check",
                "Quality / Tests",
                "success",
                quality_source,
                VALID_COMMIT_SHA,
            )
        ]
        if with_evidence
        else []
    )
    security = (
        [
            ToolEvidence(
                "ci_check",
                "Security / Dependency audit",
                "success",
                CI_SOURCE,
                security_sha,
            )
        ]
        if with_evidence
        else []
    )
    return [
        StubWorker(WorkerRole.ANALYST),
        StubWorker(WorkerRole.ARCHITECT),
        StubWorker(WorkerRole.DEVELOPER, pull_request),
        StubWorker(WorkerRole.DEBUGGER),
        StubWorker(WorkerRole.TESTER, quality),
        StubWorker(WorkerRole.SECURITY, security),
    ]


def test_orchestrator_requires_verified_ci_quality_and_security_evidence(tmp_path):
    orchestrator = AICodeOrchestrator(
        make_workers(),
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.SUCCEEDED


def test_orchestrator_does_not_certify_missing_evidence(tmp_path):
    orchestrator = AICodeOrchestrator(
        make_workers(False),
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED
    assert any("evidence" in error for error in record.errors)



def test_github_gateway_denies_repositories_outside_allowlist():
    gateway = GitHubToolGateway(object(), allowed_repositories={"trusted/only"})
    result = asyncio.run(
        gateway.execute(
            "repo.list_tree",
            {},
            make_task(),
            WorkerRole.ANALYST,
        )
    )
    assert result["ok"] is False
    assert "allowlist" in result["error"]



def test_provider_rejects_remote_plain_http():
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleProvider("http://provider.example/v1", "model")


def test_provider_allows_local_http_endpoint():
    provider = OpenAICompatibleProvider("http://127.0.0.1:8001/v1", "model")
    assert provider.endpoint.endswith("/chat/completions")


def test_provider_rejects_credentials_in_url():
    with pytest.raises(ValueError, match="credentials"):
        OpenAICompatibleProvider("https://user:pass@provider.example/v1", "model")



def test_github_gateway_blocks_sensitive_files_before_reading():
    gateway = GitHubToolGateway(
        GitHubRepositoryGateway("test-token-value-long-enough-for-gateway"),
        allowed_repositories={"strategyg/rusaai"},
    )
    result = asyncio.run(
        gateway.execute(
            "repo.read_file",
            {"path": ".env"},
            make_task(),
            WorkerRole.ANALYST,
        )
    )
    assert result["ok"] is False
    assert "sensitive" in result["error"]


def test_github_gateway_blocks_workflow_and_secret_file_writes():
    gateway = GitHubToolGateway(
        GitHubRepositoryGateway("test-token-value-long-enough-for-gateway"),
        allowed_repositories={"StrategyG/RusaAi"},
    )
    for path in (".github/workflows/ci.yml", "config/service.pem"):
        result = asyncio.run(
            gateway.execute(
                "repo.write_file",
                {"path": path, "content": "untrusted"},
                make_task(),
                WorkerRole.DEVELOPER,
            )
        )
        assert result["ok"] is False


def test_github_gateway_redacts_common_secret_formats():
    content = (
        'api_key="test-secret-value" '
        "ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "AKIA1234567890ABCDEF "
        "Bearer abcdefghijklmnopqrstuvwxyz123456"
    )
    redacted = GitHubToolGateway.redact_secrets(content)
    assert "test-secret-value" not in redacted
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in redacted
    assert "AKIA1234567890ABCDEF" not in redacted
    assert "Bearer abcdefghijklmnopqrstuvwxyz123456" not in redacted


def test_github_gateway_rejects_control_characters_in_paths():
    with pytest.raises(ValueError, match="path is invalid"):
        GitHubRepositoryGateway.validate_path("src\x00secret.py")


def test_github_gateway_fails_when_any_completed_check_failed():
    class FakeGitHub:
        async def get_check_runs(self, repository, branch):
            return VALID_COMMIT_SHA, [
                {"name": "Lint, test, and audit dependencies", "status": "completed",
                 "conclusion": "success", "html_url": CI_SOURCE},
                {"name": "Build and smoke-test container", "status": "completed",
                 "conclusion": "failure", "html_url": CI_SOURCE},
            ]

    gateway = GitHubToolGateway(FakeGitHub(), allowed_repositories={"strategyg/rusaai"})
    result = asyncio.run(
        gateway.execute("ci.wait_for_checks", {"wait_seconds": 0}, make_task(), WorkerRole.TESTER)
    )
    assert result["ok"] is False
    assert "unsuccessful" in result["error"]


def test_github_gateway_redacts_environment_style_secret_names():
    content = "RUSA_API_KEY=super-secret GITHUB_TOKEN: another-secret"
    redacted = GitHubToolGateway.redact_secrets(content)
    assert "super-secret" not in redacted
    assert "another-secret" not in redacted


def test_worker_rejects_truthy_string_success():
    provider = FakeProvider([{
        "summary": "false-positive response",
        "final": True,
        "success": "false",
        "tool_calls": [],
        "blockers": [],
    }])
    result = asyncio.run(
        AgentWorker(WorkerRole.DEVELOPER, provider, FakeTools()).run(make_task(), [])
    )
    assert result.success is False
    assert any("JSON booleans" in blocker for blocker in result.blockers)



def test_orchestrator_rejects_ci_evidence_from_different_commits(tmp_path):
    orchestrator = AICodeOrchestrator(
        make_workers(security_sha="b" * 40),
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED
    assert any("not bound" in error for error in record.errors)


def test_orchestrator_rejects_non_github_ci_evidence_sources(tmp_path):
    orchestrator = AICodeOrchestrator(
        make_workers(quality_source="https://attacker.example/StrategyG/RusaAi/runs/123"),
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED


def test_github_gateway_rejects_non_official_api_host():
    with pytest.raises(ValueError, match="api.github.com"):
        GitHubRepositoryGateway("x" * 40, api_url="https://attacker.example")


def test_github_gateway_refuses_to_reuse_a_diverged_work_branch(monkeypatch):
    gateway = GitHubRepositoryGateway("x" * 40)

    async def fake_request(method, path, **kwargs):
        if method == "GET" and path.endswith("/branches/main"):
            return {"commit": {"sha": "a" * 40}}
        if method == "POST":
            raise GitHubAPIError(422, "reference already exists")
        if method == "GET" and path.endswith("/branches/feature%2Faicode-test"):
            return {"commit": {"sha": "b" * 40}}
        raise AssertionError(f"unexpected GitHub request: {method} {path}")

    monkeypatch.setattr(gateway, "_request", fake_request)
    with pytest.raises(RuntimeError, match="different head"):
        asyncio.run(
            gateway.create_branch(
                "StrategyG/RusaAi",
                "main",
                "feature/aicode-test",
            )
        )



def test_github_gateway_enforces_repository_allowlist_per_tenant():
    gateway = GitHubToolGateway(
        object(),
        tenant_repositories={
            "tenant-a": {"trusted/repo-a"},
            "tenant-b": {"trusted/repo-b"},
        },
    )
    task = make_task()
    task.tenant_id = "tenant-a"
    denied = asyncio.run(
        gateway.execute("repo.list_tree", {}, task, WorkerRole.ANALYST)
    )
    assert denied["ok"] is False
    assert "authenticated tenant" in denied["error"]

    task.repository = RepositoryTarget(
        "trusted/repo-a",
        "main",
        "feature/aicode-test",
    )
    class FakeGitHub:
        async def list_tree(self, repository, ref):
            return {"sha": VALID_COMMIT_SHA, "paths": ["README.md"], "truncated": False}

    gateway.github = FakeGitHub()
    allowed = asyncio.run(
        gateway.execute("repo.list_tree", {}, task, WorkerRole.ANALYST)
    )
    assert allowed["ok"] is True


def test_github_gateway_rejects_empty_tenant_repository_allowlist():
    with pytest.raises(ValueError, match="at least one allowed repository"):
        GitHubToolGateway(
            object(),
            tenant_repositories={"tenant-a": set()},
        )



def test_provider_requires_explicit_allowlist_for_remote_https_host():
    with pytest.raises(ValueError, match="explicitly allowlisted"):
        OpenAICompatibleProvider("https://provider.example/v1", "model")


def test_provider_accepts_explicitly_allowlisted_remote_https_host():
    provider = OpenAICompatibleProvider(
        "https://provider.example/v1",
        "model",
        allowed_hosts={"provider.example"},
    )
    assert provider.endpoint == "https://provider.example/v1/chat/completions"


def test_provider_host_allowlist_is_case_insensitive():
    provider = OpenAICompatibleProvider(
        "https://Provider.Example/v1",
        "model",
        allowed_hosts={"provider.example"},
    )
    assert provider.endpoint.endswith("/chat/completions")



def test_github_gateway_protects_nested_codeowners_and_ci_files():
    protected_paths = (
        "docs/CODEOWNERS",
        ".gitlab-ci.yml",
        ".circleci/config.yml",
        "service/Dockerfile",
        "deploy/compose.yaml",
    )
    for path in protected_paths:
        assert GitHubToolGateway.is_protected_write_path(path), path



def test_github_gateway_blocks_nested_environment_and_secret_files():
    sensitive_paths = (
        "config/.env",
        "deploy/.env.production",
        "config/secrets.yaml",
        "service/credentials.json",
        ".kube/config",
    )
    for path in sensitive_paths:
        assert GitHubToolGateway.is_sensitive_path(path), path



def test_provider_allowlist_binds_nonstandard_ports():
    with pytest.raises(ValueError, match="host and port"):
        OpenAICompatibleProvider(
            "https://provider.example:8443/v1",
            "model",
            allowed_hosts={"provider.example"},
        )
    provider = OpenAICompatibleProvider(
        "https://provider.example:8443/v1",
        "model",
        allowed_hosts={"provider.example:8443"},
    )
    assert provider.endpoint.startswith("https://provider.example:8443/")


def test_orchestrator_rejects_ci_evidence_with_nonstandard_github_port(tmp_path):
    orchestrator = AICodeOrchestrator(
        make_workers(quality_source="https://github.com:444/StrategyG/RusaAi/actions/runs/123"),
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED



def test_orchestrator_persists_cancelled_state_and_audit(tmp_path):
    worker_started = asyncio.Event()

    class BlockingWorker:
        role = WorkerRole.ANALYST

        async def run(self, task, prior_results):
            worker_started.set()
            await asyncio.Event().wait()

    class UnusedWorker:
        def __init__(self, role):
            self.role = role

        async def run(self, task, prior_results):
            return WorkerResult(role=self.role, summary="unused", success=True)

    memory = SQLiteMemory(str(tmp_path / "cancel-memory.sqlite3"))
    audit = AuditLog(str(tmp_path / "cancel-audit.jsonl"))
    orchestrator = AICodeOrchestrator(
        [BlockingWorker()]
        + [UnusedWorker(role) for role in AICodeOrchestrator.PIPELINE[1:]],
        memory=memory,
        audit=audit,
    )
    task = make_task()

    async def cancel_task():
        running = asyncio.create_task(orchestrator.run(task))
        await worker_started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(cancel_task())
    stored = memory.get(task.task_id, tenant_id=task.tenant_id)
    assert stored["state"] == TaskState.CANCELLED.value
    assert audit.verify()
    entries = [
        json.loads(line)
        for line in (tmp_path / "cancel-audit.jsonl").read_text().splitlines()
    ]
    assert any(entry["event"] == "task.cancelled" for entry in entries)



def test_github_gateway_protects_dependency_and_deployment_manifests():
    protected_paths = (
        "package.json",
        "package-lock.json",
        "services/api/Cargo.toml",
        "go.mod",
        "deploy/production.yaml",
        "terraform/main.tf",
        ".travis.yml",
    )
    for path in protected_paths:
        assert GitHubToolGateway.is_protected_write_path(path), path



def test_worker_requires_explicit_agent_enablement(monkeypatch):
    monkeypatch.setenv("RUSA_AGENT_ENABLED", "false")
    with pytest.raises(RuntimeError, match="RUSA_AGENT_ENABLED=true"):
        asyncio.run(run_worker())



def test_github_gateway_rejects_truncated_repository_tree(monkeypatch):
    gateway = GitHubRepositoryGateway("x" * 40)

    async def fake_get_branch(repository, branch):
        return {"commit": {"sha": VALID_COMMIT_SHA}}

    async def fake_request(method, path, **kwargs):
        return {"truncated": True, "tree": []}

    monkeypatch.setattr(gateway, "get_branch", fake_get_branch)
    monkeypatch.setattr(gateway, "_request", fake_request)
    with pytest.raises(RuntimeError, match="truncated"):
        asyncio.run(gateway.list_tree("StrategyG/RusaAi", "main"))


def test_orchestrator_rejects_malformed_github_evidence_port(tmp_path):
    orchestrator = AICodeOrchestrator(
        make_workers(quality_source="https://github.com:not-a-port/StrategyG/RusaAi/actions/runs/123"),
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED



def test_runtime_requires_repository_allowlist_for_single_tenant(monkeypatch):
    monkeypatch.setenv("RUSA_PROVIDER_BASE_URL", "http://127.0.0.1:8001/v1")
    monkeypatch.setenv("RUSA_PROVIDER_MODEL", "test-model")
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://rusa:secret@localhost:5432/rusa")
    monkeypatch.setenv("RUSA_TENANT_ID", "tenant-default")
    monkeypatch.delenv("RUSA_API_KEY_TENANTS_JSON", raising=False)
    monkeypatch.setenv(
        "RUSA_GITHUB_TENANT_REPOSITORIES_JSON",
        '{"tenant-other":["StrategyG/RusaAi"]}',
    )
    monkeypatch.delenv("RUSA_GITHUB_ALLOWED_REPOSITORIES", raising=False)

    with pytest.raises(RuntimeError, match="fallback tenant"):
        build_orchestrator()



def test_github_gateway_reuses_existing_branch_only_for_same_task_commits(monkeypatch):
    gateway = GitHubRepositoryGateway("x" * 40)
    task_id = str(uuid.uuid4())
    base_sha = "a" * 40
    head_sha = "b" * 40

    async def fake_get_branch(repository, branch):
        sha = base_sha if branch == "main" else head_sha
        return {"commit": {"sha": sha}}

    async def fake_request(method, path, **kwargs):
        if method == "POST":
            raise GitHubAPIError(422, "reference already exists")
        assert "/compare/" in path
        return {
            "status": "ahead",
            "ahead_by": 1,
            "commits": [
                {"commit": {"message": f"[RUSA-AI task:{task_id}] update source"}}
            ],
        }

    monkeypatch.setattr(gateway, "get_branch", fake_get_branch)
    monkeypatch.setattr(gateway, "_request", fake_request)
    result = asyncio.run(
        gateway.create_branch(
            "StrategyG/RusaAi",
            "main",
            "feature/retryable-task",
            task_id=task_id,
        )
    )
    assert result["reused"] is True
    assert result["verified_task_commits"] == 1


def test_github_gateway_refuses_to_reuse_branch_with_foreign_commits(monkeypatch):
    gateway = GitHubRepositoryGateway("x" * 40)
    task_id = str(uuid.uuid4())

    async def fake_get_branch(repository, branch):
        sha = "a" * 40 if branch == "main" else "b" * 40
        return {"commit": {"sha": sha}}

    async def fake_request(method, path, **kwargs):
        if method == "POST":
            raise GitHubAPIError(422, "reference already exists")
        return {
            "status": "ahead",
            "ahead_by": 1,
            "commits": [{"commit": {"message": "[RUSA-AI task:other] foreign change"}}],
        }

    monkeypatch.setattr(gateway, "get_branch", fake_get_branch)
    monkeypatch.setattr(gateway, "_request", fake_request)
    with pytest.raises(RuntimeError, match="not exclusively owned"):
        asyncio.run(
            gateway.create_branch(
                "StrategyG/RusaAi",
                "main",
                "feature/foreign-branch",
                task_id=task_id,
            )
        )


def test_github_gateway_tags_file_commits_with_task_id(monkeypatch):
    gateway = GitHubRepositoryGateway("x" * 40)
    task_id = str(uuid.uuid4())
    captured = {}

    async def fake_request(method, path, **kwargs):
        if method == "GET":
            raise GitHubAPIError(404, "file not found")
        captured.update(kwargs["json_body"])
        return {
            "commit": {"sha": VALID_COMMIT_SHA},
            "content": {"sha": VALID_COMMIT_SHA},
        }

    monkeypatch.setattr(gateway, "_request", fake_request)
    asyncio.run(
        gateway.write_file(
            "StrategyG/RusaAi",
            "src/example.py",
            "print('safe')",
            "feature/task-retry",
            "update file",
            task_id=task_id,
        )
    )
    assert captured["message"].startswith(f"[RUSA-AI task:{task_id}]")


def test_github_gateway_reuses_existing_task_draft_pull_request(monkeypatch):
    gateway = GitHubRepositoryGateway("x" * 40)
    task_id = str(uuid.uuid4())
    marker = f"<!-- RUSA-AI task:{task_id} -->"

    async def fake_request(method, path, **kwargs):
        if method == "POST":
            raise GitHubAPIError(422, "pull request already exists")
        return [
            {
                "number": 12,
                "draft": True,
                "body": f"implementation details\n\n{marker}",
                "html_url": "https://github.com/StrategyG/RusaAi/pull/12",
                "head": {"ref": "feature/task-retry", "sha": VALID_COMMIT_SHA},
                "base": {"ref": "main"},
            }
        ]

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = asyncio.run(
        gateway.create_pull_request(
            "StrategyG/RusaAi",
            "feature/task-retry",
            "main",
            "AICode retry",
            "implementation details",
            task_id=task_id,
        )
    )
    assert result["number"] == 12
    assert result["draft"] is True
