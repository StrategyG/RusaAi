import asyncio
import json

from rusa_agents.autonomous import AutonomousCodeWorker
from rusa_agents.contracts import CodingTask, RepositoryTarget, WorkerRole
from rusa_agents.tool_gateway import ToolResult


class StubProvider:
    def __init__(self, response):
        self.response = response

    async def complete(self, *, system_prompt, user_prompt):
        return self.response


class StubGateway:
    def __init__(self, *, diff_output=""):
        self.calls = []
        self.diff_output = diff_output

    def execute(self, action, role):
        self.calls.append((action, role))
        if action["name"] == "run_check":
            profile = action.get("arguments", {}).get("profile", "")
            return ToolResult(
                True,
                "check passed",
                {"profile": profile, "exit_code": 0, "image": "sandbox@sha256:test"},
            )
        return ToolResult(True, "src/main.py", {"count": 1})

    def git_diff(self):
        return ToolResult(True, self.diff_output, {"exit_code": 0})


def make_task():
    return CodingTask(
        objective="Inspect repository",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/test"),
        acceptance_criteria=["Report files"],
    )


def response(actions, *, message="Task completed", done=True, **extra):
    return json.dumps({
        "message": message,
        "actions": actions,
        "done": done,
        **extra,
    })


def test_autonomous_worker_executes_only_registered_tools():
    gateway = StubGateway()
    provider = StubProvider(response([{"name": "list_files", "arguments": {"path": "."}}]))
    worker = AutonomousCodeWorker(WorkerRole.ANALYST, provider, gateway)
    result = asyncio.run(worker.run(make_task(), []))
    assert result.success
    assert len(gateway.calls) == 1


def test_autonomous_worker_rejects_role_escalation():
    gateway = StubGateway()
    provider = StubProvider(response([
        {"name": "write_file", "arguments": {"path": "main.py", "content": "x"}}
    ]))
    worker = AutonomousCodeWorker(WorkerRole.ANALYST, provider, gateway)
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert result.blockers


def test_autonomous_worker_rejects_non_json_model_output():
    worker = AutonomousCodeWorker(
        WorkerRole.ANALYST, StubProvider("Here is my analysis"), StubGateway()
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert "JSON" in result.blockers[0]


def test_autonomous_worker_stops_on_explicit_blocker():
    worker = AutonomousCodeWorker(
        WorkerRole.ANALYST,
        StubProvider(response(
            [],
            message="Repository access denied",
            blocked=True,
            blockers=["Access denied"],
        )),
        StubGateway(),
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert result.blockers == ["Access denied"]


def test_autonomous_worker_cannot_supply_forged_test_evidence():
    worker = AutonomousCodeWorker(
        WorkerRole.TESTER,
        StubProvider(response(
            [],
            message="All tests passed",
            evidence={"check:pytest": {"profile": "pytest", "exit_code": 0}},
        )),
        StubGateway(),
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert not result.evidence


def test_developer_cannot_finish_without_a_real_workspace_diff():
    gateway = StubGateway(diff_output="")
    worker = AutonomousCodeWorker(
        WorkerRole.DEVELOPER,
        StubProvider(response([
            {"name": "write_file", "arguments": {"path": "src/main.py", "content": "print('x')"}}
        ])),
        gateway,
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert "non-empty Git diff" in result.blockers[0]


def test_developer_success_requires_a_write_and_non_empty_diff():
    gateway = StubGateway(diff_output="diff --git a/src/main.py b/src/main.py\n+print('x')\n")
    worker = AutonomousCodeWorker(
        WorkerRole.DEVELOPER,
        StubProvider(response([
            {"name": "write_file", "arguments": {"path": "src/main.py", "content": "print('x')"}}
        ])),
        gateway,
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert result.success
    assert result.artifacts == ["src/main.py"]
    assert result.evidence["workspace_diff"]["present"] is True


def test_tester_cannot_finish_without_running_pytest():
    worker = AutonomousCodeWorker(
        WorkerRole.TESTER,
        StubProvider(response([{"name": "list_files", "arguments": {"path": "."}}])),
        StubGateway(),
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert "pytest" in result.blockers[0]


def test_tester_success_requires_real_pytest_profile_evidence():
    worker = AutonomousCodeWorker(
        WorkerRole.TESTER,
        StubProvider(response([{"name": "run_check", "arguments": {"profile": "pytest"}}])),
        StubGateway(),
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert result.success
    assert result.evidence["check:pytest"]["profile"] == "pytest"


def test_tool_failure_cannot_be_reported_as_worker_success():
    class FailedGateway(StubGateway):
        def execute(self, action, role):
            return ToolResult(False, "file not found")

    worker = AutonomousCodeWorker(
        WorkerRole.ANALYST,
        StubProvider(response([{"name": "read_file", "arguments": {"path": "missing.py"}}])),
        FailedGateway(),
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert "unsuccessful result" in result.blockers[0]
