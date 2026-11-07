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
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def execute(self, action, role):
        self.calls.append((action, role))
        if self.results:
            return self.results.pop(0)
        return ToolResult(True, "src/main.py", {"count": 1})


def make_task():
    return CodingTask(
        objective="Inspect repository",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/test"),
        acceptance_criteria=["Report files"],
    )


def test_autonomous_worker_executes_only_registered_tools():
    gateway = StubGateway()
    provider = StubProvider(json.dumps({
        "message": "Repository inspected",
        "actions": [{"name": "list_files", "arguments": {"path": "."}}],
        "done": True,
    }))
    worker = AutonomousCodeWorker(WorkerRole.ANALYST, provider, gateway)
    result = asyncio.run(worker.run(make_task(), []))
    assert result.success
    assert len(gateway.calls) == 1


def test_autonomous_worker_rejects_role_escalation():
    gateway = StubGateway()
    provider = StubProvider(json.dumps({
        "message": "Write a file",
        "actions": [{"name": "write_file", "arguments": {"path": "main.py", "content": "x"}}],
        "done": True,
    }))
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
        StubProvider(json.dumps({
            "message": "Repository access denied",
            "actions": [],
            "done": True,
            "blocked": True,
            "blockers": ["Access denied"],
        })),
        StubGateway(),
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert result.blockers == ["Access denied"]


def test_autonomous_worker_cannot_supply_forged_test_evidence():
    worker = AutonomousCodeWorker(
        WorkerRole.TESTER,
        StubProvider(json.dumps({
            "message": "All tests passed",
            "actions": [],
            "done": True,
            "evidence": {"check:pytest": {"exit_code": 0}},
        })),
        StubGateway(),
    )
    result = asyncio.run(worker.run(make_task(), []))
    assert not result.success
    assert not result.evidence


def test_autonomous_worker_does_not_report_success_after_failed_tool():
    gateway = StubGateway([ToolResult(False, "File not found")])
    provider = StubProvider(json.dumps({
        "message": "Inspection complete",
        "actions": [{"name": "read_file", "arguments": {"path": "missing.py"}}],
        "done": True,
    }))
    result = asyncio.run(AutonomousCodeWorker(WorkerRole.ANALYST, provider, gateway).run(make_task(), []))
    assert not result.success
    assert result.failure_kind == "tool"
    assert any("File not found" in blocker for blocker in result.blockers)


def test_autonomous_worker_retries_transient_tool_failure():
    gateway = StubGateway([
        ToolResult(False, "temporary unavailable", retryable=True),
        ToolResult(True, "src/main.py", {"count": 1}),
    ])
    provider = StubProvider(json.dumps({
        "message": "Inspection complete",
        "actions": [{"name": "list_files", "arguments": {"path": "."}}],
        "done": True,
    }))
    result = asyncio.run(AutonomousCodeWorker(WorkerRole.ANALYST, provider, gateway).run(make_task(), []))
    assert result.success
    assert len(gateway.calls) == 2


def test_autonomous_worker_preserves_failed_check_evidence_for_repair():
    gateway = StubGateway([
        ToolResult(False, "FAILED test_parser.py::test_invalid_input", {
            "profile": "pytest", "exit_code": 1, "workspace_digest": "a" * 64
        })
    ])
    provider = StubProvider(json.dumps({
        "message": "Tests need repair",
        "actions": [{"name": "run_check", "arguments": {"profile": "pytest"}}],
        "done": True,
    }))
    result = asyncio.run(AutonomousCodeWorker(WorkerRole.TESTER, provider, gateway).run(make_task(), []))
    assert not result.success
    assert result.failure_kind == "tool"
    assert result.evidence["check:pytest"]["exit_code"] == 1
    assert "FAILED test_parser.py" in result.blockers[0]


def test_autonomous_worker_does_not_clear_failure_for_a_different_action_argument():
    gateway = StubGateway([
        ToolResult(False, "File not found: missing.py"),
        ToolResult(True, "src/main.py", {"count": 1}),
    ])
    provider = StubProvider(json.dumps({
        "message": "Both reads attempted",
        "actions": [
            {"name": "read_file", "arguments": {"path": "missing.py"}},
            {"name": "read_file", "arguments": {"path": "main.py"}},
        ],
        "done": True,
    }))
    result = asyncio.run(AutonomousCodeWorker(WorkerRole.ANALYST, provider, gateway).run(make_task(), []))
    assert not result.success
    assert result.failure_kind == "tool"
    assert len(result.blockers) == 1
    assert "missing.py" in result.blockers[0]


def test_autonomous_worker_does_not_report_unchanged_write_as_artifact():
    gateway = StubGateway([ToolResult(True, "No changes required", {"changed": False})])
    provider = StubProvider(json.dumps({
        "message": "No changes needed",
        "actions": [{"name": "write_file", "arguments": {"path": "src/main.py", "content": "same"}}],
        "done": True,
    }))
    result = asyncio.run(AutonomousCodeWorker(WorkerRole.DEVELOPER, provider, gateway).run(make_task(), []))
    assert result.success
    assert result.artifacts == []
