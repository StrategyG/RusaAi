import asyncio
import json
from collections import deque

import pytest

from rusa_agents.audit import AuditIntegrityError, HMACAuditLog
from rusa_agents.contracts import (
    CodingTask, RepositoryTarget, RiskLevel, TaskRecord, TaskState,
    ToolCall, ToolResult, WorkerResult, WorkerRole,
)
from rusa_agents.github import GitHubToolGateway
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.policy import AutonomyPolicy
from rusa_agents.provider import OpenAICompatibleProvider
from rusa_agents.workers import AgentWorker


def make_task() -> CodingTask:
    return CodingTask(
        tenant_id="tenant-a",
        objective="Improve repository quality",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch="feature/agent-test",
        ),
        acceptance_criteria=("Tests pass", "Security review passes"),
        idempotency_key="test-idempotency-key",
    )


class FakeProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.last_system = ""
        self.last_user = ""

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.last_system = system_prompt
        self.last_user = user_prompt
        return self.response


class ScriptedWorker:
    def __init__(self, role: WorkerRole, results: list[WorkerResult]) -> None:
        self.role = role
        self.results = deque(results)

    async def run(self, task, prior_results):
        if not self.results:
            return worker_result(self.role)
        return self.results.popleft()


class FakeTools:
    def __init__(self, ci_outcomes: list[str] | None = None) -> None:
        self.ci_outcomes = deque(ci_outcomes or ["passed"])
        self.calls = []

    async def execute(self, task, call):
        self.calls.append((call.name, dict(call.arguments)))
        if call.name == "get_ci_status":
            outcome = self.ci_outcomes.popleft() if self.ci_outcomes else "passed"
            checks = []
            for name in (
                "Lint, test, and audit dependencies",
                "Build and smoke-test container",
            ):
                checks.append({
                    "name": name,
                    "status": "completed" if outcome != "pending" else "in_progress",
                    "conclusion": "success" if outcome == "passed" else (
                        "failure" if outcome == "failed" else None
                    ),
                })
            return ToolResult(
                name=call.name,
                success=True,
                data={"checks": checks, "all_checks_success": outcome == "passed"},
                evidence={"source": "fake-ci"},
            )
        return ToolResult(
            name=call.name,
            success=True,
            data={"name": call.name},
            evidence={"source": "fake-tool"},
        )


class FakeAudit:
    def __init__(self, valid=True) -> None:
        self.events = []
        self.valid = valid

    def record(self, event, *, tenant_id, task_id, actor, details=None):
        self.events.append((event, tenant_id, task_id, actor, details or {}))

    def verify(self):
        return self.valid


def worker_result(role, calls=(), risk=RiskLevel.LOW, blockers=()):
    return WorkerResult(
        role=role,
        summary=f"{role.value} completed",
        tool_calls=tuple(calls),
        risk=risk,
        blockers=tuple(blockers),
        criteria_results={"Tests pass": True, "Security review passes": True},
    )


def make_orchestrator(tools, overrides=None, audit=None):
    overrides = overrides or {}
    workers = []
    for role in WorkerRole:
        workers.append(overrides.get(role, ScriptedWorker(role, [worker_result(role)])))
    return AICodeOrchestrator(
        workers=workers,
        tools=tools,
        audit=audit or FakeAudit(),
        worker_timeout_seconds=2,
        max_repair_cycles=2,
    )


def test_task_contract_round_trip_preserves_tenant_and_repository():
    task = make_task()
    restored = CodingTask.from_dict(task.to_dict())
    assert restored.task_id == task.task_id
    assert restored.tenant_id == task.tenant_id
    assert restored.repository.full_name == task.repository.full_name


def test_repository_target_rejects_writes_to_base_branch():
    with pytest.raises(ValueError):
        RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch="main",
        )


def test_repository_target_rejects_invalid_branch_name():
    with pytest.raises(ValueError):
        RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch="../main",
        )


def test_policy_allows_low_risk_branch_write():
    decision = AutonomyPolicy().evaluate(
        "repo_write",
        repository=make_task().repository,
        path="rusa_agents/example.py",
    )
    assert decision.allowed is True
    assert decision.requires_approval is False


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt", "/etc/passwd", ".git/config", ".env",
        ".github/workflows/ci.yml", ".", "src//module.py",
    ],
)
def test_policy_rejects_protected_or_unsafe_paths(path):
    assert AutonomyPolicy.safe_repository_path(path) is False


def test_policy_requires_approval_for_high_risk():
    decision = AutonomyPolicy().evaluate(
        "repo_write",
        risk=RiskLevel.HIGH,
        repository=make_task().repository,
        path="rusa_agents/core.py",
    )
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_task_record_rejects_invalid_state_transition():
    record = TaskRecord(task=make_task())
    with pytest.raises(ValueError):
        record.transition(TaskState.SUCCEEDED)


def test_worker_contract_rejects_tool_outside_role_allowlist():
    provider = FakeProvider(json.dumps({
        "summary": "Read analysis",
        "tool_calls": [{"name": "write_file", "arguments": {"path": "x.py", "content": "x"}}],
        "blockers": [],
        "risk": "low",
        "criteria_results": {"Tests pass": True, "Security review passes": True},
    }))
    worker = AgentWorker(WorkerRole.ANALYST, provider)
    with pytest.raises(ValueError):
        asyncio.run(worker.run(make_task(), []))


def test_worker_contract_accepts_structured_read_tool():
    provider = FakeProvider(json.dumps({
        "summary": "Repository inspected",
        "tool_calls": [{"name": "list_files", "arguments": {"path": "rusa_agents"}}],
        "blockers": [],
        "risk": "low",
        "criteria_results": {"Tests pass": True, "Security review passes": True},
    }))
    worker = AgentWorker(WorkerRole.ANALYST, provider)
    result = asyncio.run(worker.run(make_task(), []))
    assert result.role == WorkerRole.ANALYST
    assert result.tool_calls[0].name == "list_files"
    assert "untrusted" in provider.last_system


def test_worker_contract_requires_exact_acceptance_criteria():
    provider = FakeProvider(json.dumps({
        "summary": "Repository inspected",
        "tool_calls": [],
        "blockers": [],
        "risk": "low",
        "criteria_results": {"Tests pass": True},
    }))
    worker = AgentWorker(WorkerRole.ANALYST, provider)
    with pytest.raises(ValueError):
        asyncio.run(worker.run(make_task(), []))


def test_orchestrator_completes_only_with_ci_and_security_evidence():
    developer = ScriptedWorker(WorkerRole.DEVELOPER, [worker_result(
        WorkerRole.DEVELOPER,
        [
            ToolCall("create_branch"),
            ToolCall("write_file", {"path": "rusa_agents/new.py", "content": "pass"}),
            ToolCall("create_pull_request", {"title": "Agent change"}),
        ],
    )])
    tester = ScriptedWorker(WorkerRole.TESTER, [worker_result(
        WorkerRole.TESTER, [ToolCall("get_ci_status")]
    )])
    orchestrator = make_orchestrator(FakeTools(["passed"]), {
        WorkerRole.DEVELOPER: developer,
        WorkerRole.TESTER: tester,
    })
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.SUCCEEDED
    assert record.stage_index == 7


def test_orchestrator_checkpoints_while_ci_is_pending_then_resumes():
    tester = ScriptedWorker(WorkerRole.TESTER, [
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
    ])
    tools = FakeTools(["pending", "passed"])
    orchestrator = make_orchestrator(tools, {WorkerRole.TESTER: tester})
    task = make_task()
    first = asyncio.run(orchestrator.run(task))
    assert first.state == TaskState.WAITING_EXTERNAL
    assert first.stage_index == 3
    restored = TaskRecord.from_dict(first.to_dict())
    second = asyncio.run(orchestrator.run(task, restored))
    assert second.state == TaskState.SUCCEEDED


def test_orchestrator_runs_bounded_debug_and_repair_after_failed_ci():
    developer = ScriptedWorker(WorkerRole.DEVELOPER, [
        worker_result(WorkerRole.DEVELOPER, [ToolCall("create_branch")]),
        worker_result(WorkerRole.DEVELOPER, [ToolCall(
            "write_file", {"path": "rusa_agents/fix.py", "content": "fixed"}
        )]),
    ])
    tester = ScriptedWorker(WorkerRole.TESTER, [
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
    ])
    debugger = ScriptedWorker(WorkerRole.DEBUGGER, [worker_result(
        WorkerRole.DEBUGGER, [ToolCall("get_ci_status")]
    )])
    tools = FakeTools(["failed", "passed"])
    orchestrator = make_orchestrator(tools, {
        WorkerRole.DEVELOPER: developer,
        WorkerRole.TESTER: tester,
        WorkerRole.DEBUGGER: debugger,
    })
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.SUCCEEDED
    assert record.repair_cycles == 1


def test_orchestrator_blocks_high_risk_task_for_human_approval():
    analyst = ScriptedWorker(WorkerRole.ANALYST, [
        worker_result(WorkerRole.ANALYST, risk=RiskLevel.CRITICAL)
    ])
    orchestrator = make_orchestrator(FakeTools(), {WorkerRole.ANALYST: analyst})
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.AWAITING_APPROVAL


def test_orchestrator_blocks_when_acceptance_criteria_are_not_verified():
    tester = ScriptedWorker(WorkerRole.TESTER, [WorkerResult(
        role=WorkerRole.TESTER,
        summary="CI passed but acceptance criteria are not verified",
        tool_calls=(ToolCall("get_ci_status"),),
        criteria_results={"Tests pass": True, "Security review passes": False},
    )])
    orchestrator = make_orchestrator(FakeTools(["passed"]), {WorkerRole.TESTER: tester})
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED


def test_orchestrator_blocks_when_audit_integrity_fails():
    orchestrator = make_orchestrator(FakeTools(["passed"]), audit=FakeAudit(valid=False))
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED
    assert "audit integrity" in record.errors[-1]


def test_hmac_audit_log_verifies_and_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = HMACAuditLog(str(path), "x" * 48)
    audit.record("task.started", tenant_id="tenant-a", task_id="task-1", actor="master")
    assert audit.verify() is True
    path.write_text(path.read_text().replace("task.started", "task.changed"))
    with pytest.raises(AuditIntegrityError):
        audit.verify()


def test_provider_rejects_insecure_non_loopback_url():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(
            base_url="http://provider.example.com/v1",
            api_key="secret",
            model="model",
        )


def test_provider_rejects_localhost_suffix_lookalike():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(
            base_url="http://localhost.attacker.example/v1",
            api_key="secret",
            model="model",
        )


def test_provider_allows_https_url():
    provider = OpenAICompatibleProvider(
        base_url="https://provider.example.com/v1",
        api_key="secret",
        model="model",
    )
    assert provider.base_url == "https://provider.example.com/v1"


def test_github_gateway_requires_https():
    with pytest.raises(ValueError):
        GitHubToolGateway(token="token", api_url="http://api.github.com")


def test_agent_tools_do_not_expose_merge_or_deployment():
    for role_tools in AgentWorker.ROLE_TOOLS.values():
        assert "merge" not in role_tools
        assert "deploy" not in role_tools
