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
    task_id = "agent-test-task-001"
    idempotency_key = "test-idempotency-key"
    return CodingTask(
        tenant_id="tenant-a",
        objective="Improve repository quality",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=CodingTask.expected_work_branch_for(
                "tenant-a", idempotency_key, task_id,
            ),
        ),
        acceptance_criteria=("Tests pass", "Security review passes"),
        idempotency_key=idempotency_key,
        task_id=task_id,
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

    async def run(self, task, prior_results, *, review_only=False):
        if review_only:
            return worker_result(self.role)
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
                "Build and smoke-test containers",
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
                data={
                    "head_sha": "a" * 40,
                    "checks": checks,
                    "checks_complete": True,
                    "total_count": len(checks),
                    "all_checks_success": outcome == "passed",
                },
                evidence={"source": "github_check_runs_api", "head_sha": "a" * 40},
            )
        if call.name == "get_branch_diff":
            return ToolResult(
                name=call.name,
                success=True,
                data={
                    "base_sha": "b" * 40,
                    "head_sha": "a" * 40,
                    "changed_files": 1,
                    "files": [{
                        "path": "rusa_agents/example.py",
                        "status": "modified",
                        "patch": "+return True",
                    }],
                    "diff_complete": True,
                    "sensitive_content_detected": False,
                },
                evidence={"source": "github_compare_api", "head_sha": "a" * 40},
            )
        if call.name == "create_pull_request":
            return ToolResult(
                name=call.name,
                success=True,
                data={
                    "number": 1,
                    "url": "https://github.com/StrategyG/RusaAi/pull/1",
                    "draft": True,
                },
                evidence={"source": "github_pull_requests_api"},
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
        if role == WorkerRole.SECURITY:
            default_result = worker_result(
                role,
                [ToolCall("get_ci_status"), ToolCall("get_branch_diff")],
            )
        elif role == WorkerRole.DEVELOPER:
            default_result = worker_result(
                role,
                [
                    ToolCall("create_branch"),
                    ToolCall("create_pull_request", {"title": "RUSA-AI autonomous change"}),
                ],
            )
        elif role == WorkerRole.TESTER:
            default_result = worker_result(role, [ToolCall("get_ci_status")])
        else:
            default_result = worker_result(role)
        workers.append(overrides.get(role, ScriptedWorker(role, [default_result])))
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
        worker_result(WorkerRole.DEVELOPER, [
            ToolCall("create_branch"),
            ToolCall("create_pull_request", {"title": "RUSA-AI repair"}),
        ]),
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
    tester = ScriptedWorker(WorkerRole.TESTER, [worker_result(
        WorkerRole.TESTER, [ToolCall("get_ci_status")]
    )])
    orchestrator = make_orchestrator(
        FakeTools(["passed"]), {WorkerRole.TESTER: tester}, audit=FakeAudit(valid=False)
    )
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


@pytest.mark.parametrize("path", [
    ".env.production", "config/.env", "config/.env.local",
    "config/.ENV.production", "config/.git/config", "config/.GIT/config",
])
def test_policy_blocks_nested_secrets_and_git_metadata(path):
    assert not AutonomyPolicy.safe_repository_path(path)


@pytest.mark.parametrize("operation", ["read_file", "write_file"])
def test_gateway_rejects_secret_paths_before_network(monkeypatch, operation):
    gateway = GitHubToolGateway(token="test-token", allowed_repositories_by_tenant={"tenant-a": {"StrategyG/RusaAi"}})

    async def no_network(*args, **kwargs):
        pytest.fail("Protected paths must be rejected before a GitHub request")

    monkeypatch.setattr(gateway, "_request", no_network)
    with pytest.raises(PermissionError):
        asyncio.run(gateway.execute(make_task(), ToolCall(
            operation, {"path": "config/.env.production", "content": "secret"},
        )))


@pytest.mark.parametrize("total_count,expected", [(1, True), (2, False), (None, False)])
def test_ci_cannot_pass_with_incomplete_check_results(monkeypatch, total_count, expected):
    gateway = GitHubToolGateway(token="test-token", allowed_repositories_by_tenant={"tenant-a": {"StrategyG/RusaAi"}})

    async def request(method, path, **kwargs):
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        return {
            "total_count": total_count,
            "check_runs": [{"id": 1, "name": "tests", "status": "completed", "conclusion": "success"}],
        }

    monkeypatch.setattr(gateway, "_request", request)
    result = asyncio.run(gateway.execute(make_task(), ToolCall("get_ci_status")))
    assert result.data["all_checks_success"] is expected


def test_orchestrator_waits_for_complete_ci_evidence():
    orchestrator = make_orchestrator(FakeTools())
    result = asyncio.run(FakeTools().execute(make_task(), ToolCall("get_ci_status")))
    incomplete = ToolResult(
        name=result.name, success=True,
        data={**result.data, "checks_complete": False}, evidence=result.evidence,
    )
    assert orchestrator._ci_outcome(incomplete) == "pending"
    assert orchestrator._ci_outcome(result) == "passed"


def test_default_required_checks_match_repository_workflow():
    from pathlib import Path

    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    for name in make_orchestrator(FakeTools()).required_ci_checks:
        assert f"name: {name}\n" in workflow

def test_github_gateway_rejects_repository_outside_tenant_allowlist():
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories_by_tenant={"tenant-a": {"StrategyG/RusaAi"}},
    )
    other_repository_task = CodingTask(
        tenant_id="tenant-a",
        objective="Attempt access outside the allowlist",
        repository=RepositoryTarget(
            full_name="another-owner/private-repo",
            base_branch="main",
            work_branch="feature/blocked",
        ),
        acceptance_criteria=("Access is denied",),
    )
    with pytest.raises(PermissionError, match="not allowlisted"):
        asyncio.run(gateway.execute(other_repository_task, ToolCall("list_files")))



@pytest.mark.parametrize("tamper", ["sha", "source", "failure"])
def test_ci_gate_rejects_unverifiable_evidence(tamper):
    from dataclasses import replace

    result = asyncio.run(FakeTools().execute(make_task(), ToolCall("get_ci_status")))
    if tamper == "sha":
        result = replace(result, evidence={**result.evidence, "head_sha": "b" * 40})
    elif tamper == "source":
        result = replace(result, evidence={**result.evidence, "source": "model_claim"})
    else:
        result = replace(result, success=False)
    assert make_orchestrator(FakeTools())._ci_outcome(result) == "pending"


def test_changed_head_after_security_requires_reverification():
    from dataclasses import replace

    class MovingHeadTools(FakeTools):
        def __init__(self):
            super().__init__()
            self.ci_reads = 0
            self.diff_reads = 0

        async def execute(self, task, call):
            result = await super().execute(task, call)
            if call.name == "get_ci_status":
                self.ci_reads += 1
                if self.ci_reads >= 3:
                    return replace(
                        result,
                        data={**result.data, "head_sha": "c" * 40},
                        evidence={**result.evidence, "head_sha": "c" * 40},
                    )
            elif call.name == "get_branch_diff":
                self.diff_reads += 1
                if self.diff_reads >= 2:
                    return replace(
                        result,
                        data={**result.data, "head_sha": "c" * 40},
                        evidence={**result.evidence, "head_sha": "c" * 40},
                    )
            return result

    tester = ScriptedWorker(WorkerRole.TESTER, [worker_result(
        WorkerRole.TESTER, [ToolCall("get_ci_status")]
    )])
    record = asyncio.run(make_orchestrator(
        MovingHeadTools(), {WorkerRole.TESTER: tester}
    ).run(make_task()))
    assert record.state == TaskState.WAITING_EXTERNAL
    assert record.stage_index == 3



@pytest.mark.parametrize(
    "path",
    [
        ".env.production",
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        "CODEOWNERS",
        "pyproject.toml",
        "requirements-agent.txt",
        "scripts/postgres/init-agent-db.sh",
        "docker-compose.yml",
        "package-lock.json",
    ],
)
def test_policy_requires_approval_for_protected_files(path):
    decision = AutonomyPolicy().evaluate(
        "repo_write",
        risk=RiskLevel.LOW,
        repository=make_task().repository,
        path=path,
    )
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_worker_can_list_repository_root_without_file_path():
    task = make_task()
    tools = FakeTools()
    analyst = ScriptedWorker(
        WorkerRole.ANALYST,
        [worker_result(WorkerRole.ANALYST, [ToolCall("list_files")])],
    )
    orchestrator = make_orchestrator(tools, {WorkerRole.ANALYST: analyst})
    record = TaskRecord(task=task)
    record.transition(TaskState.OBSERVING)
    result = asyncio.run(orchestrator._run_worker(record, WorkerRole.ANALYST))
    assert result.blockers == ()
    assert [item.name for item in result.tool_results] == ["list_files"]
    assert tools.calls[0][0] == "list_files"



def test_orchestrator_persists_stage_checkpoints():
    snapshots = []

    async def persist(record):
        snapshots.append((record.state, record.stage_index))

    result = asyncio.run(
        make_orchestrator(FakeTools()).run(
            make_task(),
            checkpoint_callback=persist,
        )
    )
    assert result.state == TaskState.SUCCEEDED, result.errors
    assert snapshots
    assert (TaskState.SUCCEEDED, 7) in snapshots


def test_checkpoint_persistence_failure_is_not_reported_as_task_success():
    from rusa_agents.orchestrator import CheckpointPersistenceError

    async def fail(_record):
        raise RuntimeError("database checkpoint unavailable")

    with pytest.raises(CheckpointPersistenceError):
        asyncio.run(
            make_orchestrator(FakeTools()).run(
                make_task(),
                checkpoint_callback=fail,
            )
        )



def test_github_gateway_branch_diff_is_complete_and_redacts_secrets(monkeypatch):
    task = make_task()
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories_by_tenant={"tenant-a": {"StrategyG/RusaAi"}},
    )

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "a" * 40}}
        if path.endswith("/branches/" + task.repository.work_branch.replace("/", "%2F")):
            return {"commit": {"sha": "b" * 40}}
        if "/compare/" in path:
            return {
                "status": "ahead",
                "behind_by": 0,
                "changed_files": 1,
                "files": [{
                    "filename": "rusa_agents/example.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "+TOKEN = 'ghp_" + "A" * 24 + "'",
                }],
            }
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = asyncio.run(gateway.execute(task, ToolCall("get_branch_diff")))
    assert result.success is True
    assert result.data["diff_complete"] is True
    assert result.data["changed_files"] == 1
    assert "[REDACTED TOKEN]" in result.data["files"][0]["patch"]


def test_github_gateway_branch_diff_fails_closed_for_missing_patch(monkeypatch):
    task = make_task()
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories_by_tenant={"tenant-a": {"StrategyG/RusaAi"}},
    )

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "a" * 40}}
        if path.endswith("/branches/" + task.repository.work_branch.replace("/", "%2F")):
            return {"commit": {"sha": "b" * 40}}
        if "/compare/" in path:
            return {
                "status": "ahead", "behind_by": 0, "changed_files": 1,
                "files": [{"filename": "rusa_agents/example.py", "status": "modified"}],
            }
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = asyncio.run(gateway.execute(task, ToolCall("get_branch_diff")))
    assert result.data["diff_complete"] is False



def test_security_stage_blocks_incomplete_repository_diff():
    class IncompleteDiffTools(FakeTools):
        async def execute(self, task, call):
            if call.name == "get_branch_diff":
                self.calls.append((call.name, dict(call.arguments)))
                return ToolResult(
                    name=call.name,
                    success=True,
                    data={
                        "head_sha": "a" * 40,
                        "changed_files": 1,
                        "files": [],
                        "diff_complete": False,
                    },
                    evidence={"source": "github_compare_api", "head_sha": "a" * 40},
                )
            return await super().execute(task, call)

    record = asyncio.run(make_orchestrator(IncompleteDiffTools()).run(make_task()))
    assert record.state == TaskState.BLOCKED
    assert any("complete" in error.lower() for error in record.errors), record.errors



@pytest.mark.parametrize(
    "path",
    [
        "rusa_agents/policy.py",
        "rusa_agents/github.py",
        "rusa_agents/orchestrator.py",
        "rusa_agents/queue.py",
        "rusa_agents/audit.py",
    ],
)
def test_agent_control_plane_modifications_require_human_review(path):
    policy = AutonomyPolicy()
    assert policy.safe_repository_path(path) is True
    decision = policy.evaluate(
        "repo_write",
        risk=RiskLevel.LOW,
        repository=make_task().repository,
        path=path,
    )
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_security_worker_review_only_mode_does_not_call_tools():
    response = json.dumps({
        "summary": "Reviewed the exact diff and CI evidence",
        "tool_calls": [],
        "blockers": [],
        "risk": "low",
        "criteria_results": {"Tests pass": True, "Security review passes": True},
    })
    provider = FakeProvider(response)
    worker = AgentWorker(WorkerRole.SECURITY, provider)
    result = asyncio.run(worker.run(make_task(), [], review_only=True))
    assert result.tool_calls == ()
    assert "review-only" in provider.last_system.lower()
    assert "changed-file patch" in provider.last_system.lower()


def test_orchestrator_revalidates_commit_after_security_review():
    class ChangesAtFinalCiTools(FakeTools):
        def __init__(self):
            super().__init__(["passed"])
            self.ci_reads = 0

        async def execute(self, task, call):
            result = await super().execute(task, call)
            if call.name == "get_ci_status":
                self.ci_reads += 1
                if self.ci_reads >= 3:
                    return ToolResult(
                        name=result.name,
                        success=True,
                        data={**result.data, "head_sha": "c" * 40},
                        evidence={"source": "github_check_runs_api", "head_sha": "c" * 40},
                    )
            return result

    record = asyncio.run(make_orchestrator(ChangesAtFinalCiTools()).run(make_task()))
    assert record.state == TaskState.WAITING_EXTERNAL
    assert record.stage_index == 3
