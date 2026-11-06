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
from rusa_agents.github_gateway import GitHubToolGateway
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


def make_workers(with_evidence=True):
    quality = [ToolEvidence("ci_check", "Quality / Tests", "success")] if with_evidence else []
    security = [ToolEvidence("ci_check", "Security / Dependency audit", "success")] if with_evidence else []
    return [
        StubWorker(WorkerRole.ANALYST),
        StubWorker(WorkerRole.ARCHITECT),
        StubWorker(WorkerRole.DEVELOPER),
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
