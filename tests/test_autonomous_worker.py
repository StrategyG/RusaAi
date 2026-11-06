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
    def __init__(self):
        self.calls = []

    def execute(self, action, role):
        self.calls.append((action, role))
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
