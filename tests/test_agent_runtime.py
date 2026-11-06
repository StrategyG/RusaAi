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
    task_id = "test-task-id"
    key = "test-idempotency-key"
    branch = CodingTask.expected_work_branch_for("tenant-a", key, task_id)
    return CodingTask(
        tenant_id="tenant-a",
        objective="Improve repository quality",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=branch,
        ),
        acceptance_criteria=("Tests pass", "Security review passes"),
        idempotency_key=key,
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
        self.allowed_tools = frozenset(AgentWorker.ROLE_TOOLS[role])
        self.results = deque(results)
        self.calls = 0

    async def run(self, task, prior_results, *, review_only=False):
        self.calls += 1
        if not self.results:
            return worker_result(self.role)
        return self.results.popleft()


class FakeTools:
    def __init__(self, ci_outcomes: list[str] | None = None, head_shas: list[str] | None = None) -> None:
        self.ci_outcomes = deque(ci_outcomes or ["passed"])
        self.head_shas = deque(head_shas or ["a" * 40])
        self.calls = []

    async def execute(self, task, call):
        self.calls.append((call.name, dict(call.arguments)))
        if call.name == "get_ci_status":
            outcome = self.ci_outcomes.popleft() if self.ci_outcomes else "passed"
            sha = self.head_shas.popleft() if self.head_shas else "a" * 40
            checks = [
                {
                    "name": "Lint, test, and audit dependencies",
                    "status": "completed" if outcome != "pending" else "in_progress",
                    "conclusion": "success" if outcome == "passed" else (
                        "failure" if outcome == "failed" else None
                    ),
                },
                {
                    "name": "Build and smoke-test containers",
                    "status": "completed" if outcome != "pending" else "in_progress",
                    "conclusion": "success" if outcome == "passed" else (
                        "failure" if outcome == "failed" else None
                    ),
                },
            ]
            return ToolResult(
                name=call.name,
                success=True,
                data={
                    "head_sha": sha,
                    "checks": checks,
                    "checks_complete": True,
                    "total_count": len(checks),
                    "all_checks_success": outcome == "passed",
                },
                evidence={"source": "github_check_runs_api", "head_sha": sha},
            )
        if call.name == "get_branch_diff":
            sha = self.head_shas[0] if self.head_shas else "a" * 40
            return ToolResult(
                name=call.name,
                success=True,
                data={
                    "base_sha": "b" * 40,
                    "head_sha": sha,
                    "changed_files": 1,
                    "files": [{"path": "rusa_agents/example.py", "patch": "@@ -1 +1 @@\n+safe"}],
                    "diff_complete": True,
                },
                evidence={"source": "github_compare_api", "head_sha": sha},
            )
        if call.name == "create_pull_request":
            return ToolResult(
                name=call.name,
                success=True,
                data={
                    "number": 123,
                    "url": "https://github.com/StrategyG/RusaAi/pull/123",
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
        criteria_evidence={
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
    )


def make_orchestrator(tools, overrides=None, audit=None):
    overrides = overrides or {}
    workers = []
    for role in WorkerRole:
        if role in overrides:
            workers.append(overrides[role])
        elif role == WorkerRole.DEVELOPER:
            workers.append(ScriptedWorker(role, [
                worker_result(role, [ToolCall("create_pull_request", {"title": "Task PR"})])
            ]))
        elif role == WorkerRole.TESTER:
            workers.append(ScriptedWorker(role, [
                worker_result(role, [ToolCall("get_ci_status")])
            ]))
        elif role == WorkerRole.SECURITY:
            workers.append(ScriptedWorker(role, [
                worker_result(role, [
                    ToolCall("get_ci_status"),
                    ToolCall("get_branch_diff"),
                ])
            ]))
        else:
            workers.append(ScriptedWorker(role, [worker_result(role)]))
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
    assert restored.repository.work_branch == task.expected_work_branch()


def test_task_contract_rejects_non_isolated_branch():
    with pytest.raises(ValueError, match="tenant-scoped"):
        CodingTask(
            tenant_id="tenant-a",
            objective="Change repository",
            repository=RepositoryTarget(
                full_name="StrategyG/RusaAi",
                base_branch="main",
                work_branch="feature/shared-branch",
            ),
            acceptance_criteria=("Tests pass",),
        )


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
        "criteria_evidence": {
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
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
        "criteria_evidence": {
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
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
        "criteria_evidence": {"Tests pass": "tool:read_file:inspected"},
    }))
    worker = AgentWorker(WorkerRole.ANALYST, provider)
    with pytest.raises(ValueError):
        asyncio.run(worker.run(make_task(), []))


def test_security_worker_is_instructed_to_review_diff_and_ci():
    provider = FakeProvider(json.dumps({
        "summary": "Security review",
        "tool_calls": [
            {"name": "get_ci_status", "arguments": {}},
            {"name": "get_branch_diff", "arguments": {}},
        ],
        "blockers": [],
        "risk": "low",
        "criteria_results": {"Tests pass": True, "Security review passes": True},
        "criteria_evidence": {
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
    }))
    worker = AgentWorker(WorkerRole.SECURITY, provider)
    result = asyncio.run(worker.run(make_task(), []))
    assert {call.name for call in result.tool_calls} == {"get_ci_status", "get_branch_diff"}
    assert "MUST call get_branch_diff and get_ci_status" in provider.last_system


def test_orchestrator_completes_only_with_ci_and_same_sha_security_evidence():
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
    security = ScriptedWorker(WorkerRole.SECURITY, [worker_result(
        WorkerRole.SECURITY, [
            ToolCall("get_ci_status"),
            ToolCall("get_branch_diff"),
        ]
    )])
    orchestrator = make_orchestrator(
        FakeTools(["passed", "passed"]),
        {
            WorkerRole.DEVELOPER: developer,
            WorkerRole.TESTER: tester,
            WorkerRole.SECURITY: security,
        },
    )
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.SUCCEEDED
    assert record.stage_index == 7


def test_orchestrator_checkpoints_while_ci_is_pending_then_resumes():
    tester = ScriptedWorker(WorkerRole.TESTER, [
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
    ])
    tools = FakeTools(["pending", "passed", "passed"])
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
            ToolCall("create_pull_request", {"title": "Agent change"}),
        ]),
        worker_result(WorkerRole.DEVELOPER, [
            ToolCall("write_file", {"path": "rusa_agents/fix.py", "content": "fixed"})
        ]),
    ])
    tester = ScriptedWorker(WorkerRole.TESTER, [
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
        worker_result(WorkerRole.TESTER, [ToolCall("get_ci_status")]),
    ])
    debugger = ScriptedWorker(WorkerRole.DEBUGGER, [worker_result(
        WorkerRole.DEBUGGER, [ToolCall("get_ci_status")]
    )])
    tools = FakeTools(["failed", "passed", "passed"])
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
        criteria_evidence={
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:checks failed",
        },
    )])
    orchestrator = make_orchestrator(FakeTools(["passed"]), {WorkerRole.TESTER: tester})
    record = asyncio.run(orchestrator.run(make_task()))
    assert record.state == TaskState.BLOCKED


def test_orchestrator_blocks_when_audit_integrity_fails():
    tester = ScriptedWorker(WorkerRole.TESTER, [worker_result(
        WorkerRole.TESTER, [ToolCall("get_ci_status")]
    )])
    orchestrator = make_orchestrator(
        FakeTools(["passed", "passed"]),
        {WorkerRole.TESTER: tester},
        audit=FakeAudit(valid=False),
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
        GitHubToolGateway(
            token="token",
            api_url="http://api.github.com",
            allowed_repositories={"StrategyG/RusaAi"},
        )


def test_github_gateway_requires_explicit_repository_allowlist():
    with pytest.raises(ValueError):
        GitHubToolGateway(token="token", allowed_repositories=set())


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
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )

    async def no_network(*args, **kwargs):
        pytest.fail("Protected paths must be rejected before a GitHub request")

    monkeypatch.setattr(gateway, "_request", no_network)
    with pytest.raises(PermissionError, match="protected or unsafe"):
        asyncio.run(gateway.execute(make_task(), ToolCall(
            operation, {"path": "config/.env.production", "content": "secret"},
        )))


@pytest.mark.parametrize("total_count,expected", [(1, True), (2, False), (None, False)])
def test_ci_cannot_pass_with_incomplete_check_results(monkeypatch, total_count, expected):
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )

    async def request(method, path, **kwargs):
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        return {
            "total_count": total_count,
            "check_runs": [{
                "id": 1,
                "name": "Lint, test, and audit dependencies",
                "status": "completed",
                "conclusion": "success",
            }],
        }

    monkeypatch.setattr(gateway, "_request", request)
    result = asyncio.run(gateway.execute(make_task(), ToolCall("get_ci_status")))
    assert result.data["all_checks_success"] is expected


def test_orchestrator_waits_for_complete_and_authentic_ci_evidence():
    orchestrator = make_orchestrator(FakeTools())
    result = asyncio.run(FakeTools().execute(make_task(), ToolCall("get_ci_status")))
    incomplete = ToolResult(
        name=result.name,
        success=True,
        data={**result.data, "checks_complete": False},
        evidence=result.evidence,
    )
    assert orchestrator._ci_outcome(incomplete) == "pending"
    assert orchestrator._ci_outcome(result) == "passed"


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


def test_changed_head_after_tester_requires_fresh_ci_and_diff():
    tester = ScriptedWorker(WorkerRole.TESTER, [worker_result(
        WorkerRole.TESTER, [ToolCall("get_ci_status")]
    )])
    security = ScriptedWorker(WorkerRole.SECURITY, [worker_result(
        WorkerRole.SECURITY, [
            ToolCall("get_ci_status"),
            ToolCall("get_branch_diff"),
        ]
    )])
    tools = FakeTools(
        ["passed", "passed"],
        ["a" * 40, "b" * 40],
    )
    record = asyncio.run(make_orchestrator(
        tools,
        {WorkerRole.TESTER: tester, WorkerRole.SECURITY: security},
    ).run(make_task()))
    assert record.state == TaskState.WAITING_EXTERNAL
    assert record.stage_index == 3


def test_orchestrator_persists_each_checkpoint_and_propagates_checkpoint_failure():
    checkpoints = []

    async def persist(record):
        checkpoints.append((record.state.value, record.stage_index))

    task = make_task()
    record = asyncio.run(make_orchestrator(FakeTools(["passed", "passed"])).run(
        task, checkpoint_callback=persist,
    ))
    assert record.state == TaskState.SUCCEEDED
    assert any(stage == 3 for _, stage in checkpoints)
    assert checkpoints[-1] == ("succeeded", 7)

    async def fail_persist(record):
        raise RuntimeError("database unavailable")

    with pytest.raises(Exception, match="checkpoint"):
        asyncio.run(make_orchestrator(FakeTools()).run(
            make_task(), checkpoint_callback=fail_persist,
        ))


def test_default_required_checks_match_repository_workflow():
    from pathlib import Path

    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    for name in make_orchestrator(FakeTools()).required_ci_checks:
        assert f"name: {name}\n" in workflow


def test_github_gateway_rejects_repository_outside_allowlist_before_network(monkeypatch):
    task_id = "other-task-id"
    key = "other-task-key"
    branch = CodingTask.expected_work_branch_for("tenant-a", key, task_id)
    other_task = CodingTask(
        tenant_id="tenant-a",
        objective="Read another repository",
        repository=RepositoryTarget(
            full_name="other-owner/private-repo",
            base_branch="main",
            work_branch=branch,
        ),
        acceptance_criteria=("No changes",),
        idempotency_key=key,
        task_id=task_id,
    )
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )

    async def no_network(*args, **kwargs):
        pytest.fail("Repository allowlist must be enforced before network access")

    monkeypatch.setattr(gateway, "_request", no_network)
    with pytest.raises(PermissionError, match="allowlist"):
        asyncio.run(gateway.execute(other_task, ToolCall("list_files")))


def test_github_gateway_redacts_secrets_from_file_and_diff(monkeypatch):
    import base64

    task = make_task()
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )
    github_token = "ghp_" + "A" * 32
    file_text = f'api_key = "supersecret-value-123456"\ntoken = "{github_token}"\n'
    encoded = base64.b64encode(file_text.encode()).decode()

    async def request(method, path, **kwargs):
        if "/contents/" in path:
            return {
                "type": "file",
                "size": len(file_text.encode()),
                "content": encoded,
                "sha": "f" * 40,
            }
        if "/branches/main" in path:
            return {"commit": {"sha": "b" * 40}}
        if "/branches/rusa-agent/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/compare/" in path:
            return {
                "changed_files": 1,
                "files": [{
                    "filename": "src/config.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": f'+token = "{github_token}"\n',
                }],
            }
        raise AssertionError(f"unexpected GitHub path: {path}")

    monkeypatch.setattr(gateway, "_request", request)
    read = asyncio.run(gateway.execute(
        task, ToolCall("read_file", {"path": "src/config.py"}),
    ))
    diff = asyncio.run(gateway.execute(task, ToolCall("get_branch_diff")))

    assert "supersecret-value" not in read.data["content"]
    assert github_token not in read.data["content"]
    assert "[REDACTED" in read.data["content"]
    assert github_token not in diff.data["files"][0]["patch"]
    assert "[REDACTED" in diff.data["files"][0]["patch"]


def test_github_gateway_paginates_all_check_runs(monkeypatch):
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )
    first_page = [
        {"id": index, "name": f"check-{index}", "status": "completed", "conclusion": "success"}
        for index in range(1, 101)
    ]
    second_page = [{
        "id": 101,
        "name": "Lint, test, and audit dependencies",
        "status": "completed",
        "conclusion": "success",
    }]

    async def request(method, path, **kwargs):
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        page = kwargs["params"]["page"]
        return {
            "total_count": 101,
            "check_runs": first_page if page == "1" else second_page,
        }

    monkeypatch.setattr(gateway, "_request", request)
    result = asyncio.run(gateway.execute(make_task(), ToolCall("get_ci_status")))
    assert result.data["checks_complete"] is True
    assert result.data["total_count"] == 101
    assert len(result.data["checks"]) == 101
    assert result.data["all_checks_success"] is True


def test_github_gateway_fails_closed_when_check_run_total_changes(monkeypatch):
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )

    async def request(method, path, **kwargs):
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        page = kwargs["params"]["page"]
        total = 101 if page == "1" else 102
        runs = [
            {"id": index, "name": f"check-{index}", "status": "completed", "conclusion": "success"}
            for index in range(100)
        ] if page == "1" else [{
            "id": 101, "name": "last-check", "status": "completed", "conclusion": "success",
        }]
        return {"total_count": total, "check_runs": runs}

    monkeypatch.setattr(gateway, "_request", request)
    result = asyncio.run(gateway.execute(make_task(), ToolCall("get_ci_status")))
    assert result.data["checks_complete"] is False
    assert result.data["all_checks_success"] is False


def test_github_gateway_marks_oversized_security_diff_incomplete(monkeypatch):
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
        max_diff_files=20,
    )

    async def request(method, path, **kwargs):
        if "/branches/main" in path:
            return {"commit": {"sha": "b" * 40}}
        if "/branches/rusa-agent/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/compare/" in path:
            return {
                "changed_files": 21,
                "files": [
                    {
                        "filename": f"src/module_{index}.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "patch": "+safe = True",
                    }
                    for index in range(21)
                ],
            }
        raise AssertionError(f"unexpected GitHub path: {path}")

    monkeypatch.setattr(gateway, "_request", request)
    result = asyncio.run(gateway.execute(make_task(), ToolCall("get_branch_diff")))
    assert result.data["diff_complete"] is False
    assert result.data["changed_files"] == 21
    assert len(result.data["files"]) == 20


def test_github_gateway_never_returns_patch_for_protected_file(monkeypatch):
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )

    async def request(method, path, **kwargs):
        if "/branches/main" in path:
            return {"commit": {"sha": "b" * 40}}
        if "/branches/rusa-agent/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/compare/" in path:
            return {
                "changed_files": 1,
                "files": [{
                    "filename": "config/.env.production",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "+API_KEY=top-secret-value",
                }],
            }
        raise AssertionError(f"unexpected GitHub path: {path}")

    monkeypatch.setattr(gateway, "_request", request)
    result = asyncio.run(gateway.execute(make_task(), ToolCall("get_branch_diff")))
    assert result.data["diff_complete"] is False
    assert result.data["files"][0]["patch"] == ""


def test_security_review_only_mode_sees_prior_diff_and_rejects_tools():
    task = make_task()
    diff_result = ToolResult(
        name="get_branch_diff",
        success=True,
        data={
            "head_sha": "a" * 40,
            "diff_complete": True,
            "changed_files": 1,
            "files": [{
                "path": "src/example.py",
                "patch": "+dangerous_eval = eval(user_input)",
            }],
        },
        evidence={"source": "github_compare_api", "head_sha": "a" * 40},
    )
    ci_result = ToolResult(
        name="get_ci_status",
        success=True,
        data={
            "head_sha": "a" * 40,
            "checks_complete": True,
            "total_count": 2,
            "checks": [
                {"name": "Lint, test, and audit dependencies", "status": "completed", "conclusion": "success"},
                {"name": "Build and smoke-test containers", "status": "completed", "conclusion": "success"},
            ],
        },
        evidence={"source": "github_check_runs_api", "head_sha": "a" * 40},
    )
    prior = [WorkerResult(
        role=WorkerRole.SECURITY,
        summary="Fetched security evidence",
        tool_results=(ci_result, diff_result),
        criteria_results={"Tests pass": True, "Security review passes": True},
        criteria_evidence={
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
    )]
    provider = FakeProvider(json.dumps({
        "summary": "The diff contains unsafe dynamic evaluation.",
        "tool_calls": [],
        "blockers": ["Unsafe eval() requires remediation"],
        "risk": "high",
        "criteria_results": {"Tests pass": True, "Security review passes": False},
        "criteria_evidence": {
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_branch_diff:unsafe eval detected",
        },
    }))
    worker = AgentWorker(WorkerRole.SECURITY, provider)
    result = asyncio.run(worker.run(task, prior, review_only=True))
    assert not result.tool_calls
    assert "dangerous_eval = eval(user_input)" in provider.last_user
    assert "REVIEW-ONLY MODE" in provider.last_system
    assert result.blockers


def test_security_review_only_mode_rejects_any_tool_call():
    provider = FakeProvider(json.dumps({
        "summary": "Review complete",
        "tool_calls": [{"name": "get_ci_status", "arguments": {}}],
        "blockers": [],
        "risk": "low",
        "criteria_results": {"Tests pass": True, "Security review passes": True},
        "criteria_evidence": {
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
    }))
    worker = AgentWorker(WorkerRole.SECURITY, provider)
    with pytest.raises(ValueError, match="unauthorized tool"):
        asyncio.run(worker.run(make_task(), [], review_only=True))


def test_github_gateway_enforces_tenant_repository_assignment_before_network(monkeypatch):
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
        tenant_repositories={"tenant-b": {"strategyg/rusaai"}},
    )

    async def no_network(*args, **kwargs):
        pytest.fail("Tenant repository policy must run before network access")

    monkeypatch.setattr(gateway, "_request", no_network)
    with pytest.raises(PermissionError, match="assigned to this tenant"):
        asyncio.run(gateway.execute(make_task(), ToolCall("list_files")))


def test_orchestrator_reuses_checkpointed_worker_result_after_restart():
    task = make_task()
    analyst = ScriptedWorker(WorkerRole.ANALYST, [worker_result(WorkerRole.ANALYST)])
    orchestrator = make_orchestrator(
        FakeTools(["passed", "passed"]),
        {WorkerRole.ANALYST: analyst},
    )
    record = TaskRecord(task=task)
    failed_once = False

    async def fail_after_analyst_checkpoint(current):
        nonlocal failed_once
        if current.stage_index == 0 and current.worker_results and not failed_once:
            failed_once = True
            raise RuntimeError("temporary checkpoint failure")

    with pytest.raises(Exception, match="checkpoint"):
        asyncio.run(orchestrator.run(
            task,
            record,
            checkpoint_callback=fail_after_analyst_checkpoint,
        ))

    assert record.stage_index == 0
    assert record.worker_results[-1].role == WorkerRole.ANALYST
    resumed = asyncio.run(orchestrator.run(task, record))
    assert resumed.state == TaskState.SUCCEEDED
    assert analyst.calls == 1


def test_orchestrator_blocks_diff_with_detected_secret_material():
    tester = ScriptedWorker(WorkerRole.TESTER, [worker_result(
        WorkerRole.TESTER, [ToolCall("get_ci_status")]
    )])
    security = ScriptedWorker(WorkerRole.SECURITY, [worker_result(
        WorkerRole.SECURITY, [
            ToolCall("get_ci_status"),
            ToolCall("get_branch_diff"),
        ]
    )])
    tools = FakeTools(["passed", "passed"])
    original_execute = tools.execute

    async def execute_with_secret(task, call):
        result = await original_execute(task, call)
        if call.name == "get_branch_diff":
            return ToolResult(
                name="get_branch_diff",
                success=True,
                data={
                    "base_sha": "b" * 40,
                    "head_sha": "a" * 40,
                    "changed_files": 1,
                    "files": [{
                        "path": "src/config.py",
                        "patch": "+api_key = [REDACTED TOKEN]",
                    }],
                    "diff_complete": True,
                },
                evidence={"source": "github_compare_api", "head_sha": "a" * 40},
            )
        return result

    tools.execute = execute_with_secret
    record = asyncio.run(make_orchestrator(
        tools,
        {WorkerRole.TESTER: tester, WorkerRole.SECURITY: security},
    ).run(make_task()))
    assert record.state == TaskState.BLOCKED
    assert "potential secret material" in record.errors[-1]


def test_task_checkpoint_omits_raw_file_write_content_and_bounds_file_reads():
    task = make_task()
    record = TaskRecord(task=task)
    record.worker_results.append(WorkerResult(
        role=WorkerRole.DEVELOPER,
        summary="Wrote a repository file",
        tool_calls=(ToolCall(
            "write_file",
            {"path": "src/generated.py", "content": "x" * 10000},
        ),),
        tool_results=(ToolResult(
            name="read_file",
            success=True,
            data={"path": "src/generated.py", "content": "y" * 10000},
        ),),
        criteria_results={"Tests pass": True, "Security review passes": True},
        criteria_evidence={
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
    ))
    checkpoint = record.to_dict()
    call_args = checkpoint["worker_results"][0]["tool_calls"][0]["arguments"]
    read_data = checkpoint["worker_results"][0]["tool_results"][0]["data"]

    assert "content" not in call_args
    assert call_args["content_omitted"] is True
    assert call_args["content_sha256"]
    assert "content" not in read_data
    assert read_data["content_omitted"] is True
    assert read_data["content_sha256"]
    restored = TaskRecord.from_dict(checkpoint)
    assert restored.worker_results[0].tool_calls[0].arguments["content_omitted"] is True


def test_task_checkpoint_preserves_complete_ci_check_set():
    task = make_task()
    checks = [
        {
            "id": index,
            "name": f"check-{index}",
            "status": "completed",
            "conclusion": "success",
        }
        for index in range(100)
    ]
    record = TaskRecord(task=task)
    record.worker_results.append(WorkerResult(
        role=WorkerRole.TESTER,
        summary="All CI checks passed",
        tool_results=(ToolResult(
            name="get_ci_status",
            success=True,
            data={
                "head_sha": "a" * 40,
                "checks_complete": True,
                "total_count": 100,
                "checks": checks,
                "all_checks_success": True,
            },
            evidence={"source": "github_check_runs_api", "head_sha": "a" * 40},
        ),),
        criteria_results={"Tests pass": True, "Security review passes": True},
        criteria_evidence={
            "Tests pass": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
            "Security review passes": "tool:get_ci_status:passed; tool:get_branch_diff:reviewed",
        },
    ))
    restored = TaskRecord.from_dict(record.to_dict())
    ci = restored.worker_results[0].tool_results[0]
    assert len(ci.data["checks"]) == 100
    assert ci.data["total_count"] == 100


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "requirements-agent.txt",
        "pyproject.toml",
        "Dockerfile",
        "docker-compose.yml",
        "infra/production/main.tf",
        "deploy/kubernetes/service.yaml",
    ],
)
def test_policy_requires_human_approval_for_dependency_and_infrastructure_changes(path):
    decision = AutonomyPolicy().evaluate(
        "repo_write",
        repository=make_task().repository,
        path=path,
    )
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_worker_required_ci_checks_are_configurable(monkeypatch):
    from rusa_agents.worker import _load_required_ci_checks

    monkeypatch.setenv(
        "RUSA_AGENT_REQUIRED_CI_CHECKS",
        '["Unit tests","Security scan"]',
    )
    assert _load_required_ci_checks() == ("Unit tests", "Security scan")


def test_worker_rejects_invalid_required_ci_check_configuration(monkeypatch):
    from rusa_agents.worker import _load_required_ci_checks

    monkeypatch.setenv("RUSA_AGENT_REQUIRED_CI_CHECKS", "not-json")
    with pytest.raises(RuntimeError, match="JSON array"):
        _load_required_ci_checks()


def test_sensitive_text_redaction_covers_credentials_and_private_keys():
    from rusa_agents.github import redact_sensitive_text

    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-material\n-----END OPENSSH PRIVATE KEY-----"
    value = (
        'api_key = "private-value-123456"\n'
        'token = "generic-token-value-123456"\n'
        'password: "strong-password-value"\n'
        'Authorization: Bearer very-long-bearer-secret-value-1234567890\n'
        + private_key
    )
    redacted = redact_sensitive_text(value)
    assert "private-value-123456" not in redacted
    assert "generic-token-value-123456" not in redacted
    assert "strong-password-value" not in redacted
    assert "very-long-bearer-secret-value-1234567890" not in redacted
    assert "secret-material" not in redacted
    assert "[REDACTED" in redacted


def test_orchestrator_never_executes_repository_tool_without_current_lease():
    developer = ScriptedWorker(WorkerRole.DEVELOPER, [worker_result(
        WorkerRole.DEVELOPER, [ToolCall("create_branch")]
    )])
    tools = FakeTools()
    orchestrator = make_orchestrator(
        tools,
        {WorkerRole.DEVELOPER: developer},
    )

    async def lease_guard():
        return False

    record = asyncio.run(orchestrator.run(make_task(), tool_guard=lease_guard))
    assert record.state == TaskState.FAILED
    assert tools.calls == []
    assert "lease is no longer valid" in record.errors[-1]


def test_hmac_audit_log_redacts_sensitive_detail_values(tmp_path):
    path = tmp_path / "audit-redacted.jsonl"
    audit = HMACAuditLog(str(path), "audit-key-" + "x" * 48)
    audit.record(
        "task.completed",
        tenant_id="tenant-a",
        task_id="task-redaction",
        actor="master",
        details={
            "api_key": "raw-api-key-secret-value",
            "summary": 'token = "raw-token-secret-value"',
            "nested": {"password": "raw-password-secret-value"},
        },
    )
    serialized = path.read_text()
    assert "raw-api-key-secret-value" not in serialized
    assert "raw-token-secret-value" not in serialized
    assert "raw-password-secret-value" not in serialized
    assert "[REDACTED]" in serialized
    assert audit.verify() is True


def test_github_write_recovers_after_ambiguous_commit_response(monkeypatch):
    import base64

    task = make_task()
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )
    new_content = "print('durably written')\n"
    encoded = base64.b64encode(new_content.encode()).decode()
    contents_reads = 0

    async def request(method, path, **kwargs):
        nonlocal contents_reads
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "a" * 40}}
        if "/contents/" in path and method == "GET":
            contents_reads += 1
            if contents_reads == 1:
                return None
            return {
                "type": "file",
                "size": len(new_content.encode()),
                "content": encoded,
                "sha": "b" * 40,
            }
        if "/contents/" in path and method == "PUT":
            raise RuntimeError("connection lost after server commit")
        raise AssertionError(f"unexpected GitHub request: {method} {path}")

    monkeypatch.setattr(gateway, "_request", request)
    result = asyncio.run(gateway.execute(
        task,
        ToolCall("write_file", {"path": "src/recovered.py", "content": new_content}),
    ))
    assert result.success is True
    assert result.data["recovered_after_ambiguous_commit"] is True


def test_github_gateway_rejects_diff_limits_above_review_context_capacity():
    with pytest.raises(ValueError, match="invalid GitHub gateway limits"):
        GitHubToolGateway(
            token="test-token",
            allowed_repositories={"strategyg/rusaai"},
            max_diff_files=21,
        )


def test_github_write_blocks_secret_material_before_creating_branch(monkeypatch):
    gateway = GitHubToolGateway(
        token="test-token",
        allowed_repositories={"strategyg/rusaai"},
    )

    async def no_network(*args, **kwargs):
        pytest.fail("Secret-bearing file content must be blocked before GitHub access")

    monkeypatch.setattr(gateway, "_request", no_network)
    with pytest.raises(PermissionError, match="potential secret material"):
        asyncio.run(gateway.execute(
            make_task(),
            ToolCall(
                "write_file",
                {
                    "path": "src/config.py",
                    "content": 'API_KEY = "raw-secret-value-123456"',
                },
            ),
        ))


@pytest.mark.parametrize("guard_mode", ["revoked", "error"])
def test_orchestrator_fails_closed_before_repository_tool_when_lease_is_lost(guard_mode):
    from rusa_agents.orchestrator import _LeaseLost

    task = make_task()
    tools = FakeTools()
    developer = ScriptedWorker(
        WorkerRole.DEVELOPER,
        [worker_result(
            WorkerRole.DEVELOPER,
            [ToolCall(
                "write_file",
                {"path": "rusa_agents/cancelled.py", "content": "must_not_be_written"},
            )],
        )],
    )
    orchestrator = make_orchestrator(
        tools,
        {WorkerRole.DEVELOPER: developer},
    )

    async def revoked_guard():
        if guard_mode == "error":
            raise RuntimeError("database unavailable")
        return False

    with pytest.raises(_LeaseLost, match="lease is no longer valid"):
        asyncio.run(orchestrator._run_worker(
            TaskRecord(task=task),
            WorkerRole.DEVELOPER,
            None,
            revoked_guard,
        ))

    assert tools.calls == []
