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
