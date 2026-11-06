from __future__ import annotations

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select

from rusa_agents.contracts import (
    AgentPlan,
    TaskCreate,
    TaskStatus,
    WorkerMessage,
    WorkerRole,
    validate_transition,
)
from rusa_agents.memory import MemoryStore
from rusa_agents.messaging import AgentMessageBus
from rusa_agents.orchestrator import HierarchicalOrchestrator, PlanRejected
from rusa_agents.policy import AutonomyPolicy
from rusa_agents.storage import Database, TaskRow, utcnow
from rusa_agents.tools import ToolError, ToolRegistry
from rusa_agents.worker import AgentRuntime


def make_task(**overrides) -> TaskCreate:
    data = {
        "objective": "Improve agent orchestration reliability",
        "repository": "StrategyG/RusaAi",
        "work_branch": "feature/agent-runtime-test",
        "acceptance_criteria": ["All required tests pass"],
    }
    data.update(overrides)
    return TaskCreate.model_validate(data)


class FakeProvider:
    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        return '{"steps": [], "expected_outcome": "test"}'


async def no_op_tool(**kwargs):
    return {"ok": True}


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'agent-test.db'}")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def registry() -> ToolRegistry:
    tools = ToolRegistry()
    for name in {
        "github.list_files",
        "github.read_file",
        "github.create_branch",
        "github.write_file",
        "github.get_ci_status",
        "github.create_pull_request",
    }:
        tools.register(name, no_op_tool)
    return tools


def valid_plan() -> AgentPlan:
    return AgentPlan.model_validate(
        {
            "steps": [
                {
                    "tool": "github.list_files",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "path": "",
                        "ref": "main",
                    },
                    "purpose": "Inspect repository structure",
                },
                {
                    "tool": "github.create_branch",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "base_branch": "main",
                        "work_branch": "feature/agent-runtime-test",
                    },
                    "purpose": "Create isolated work branch",
                },
                {
                    "tool": "github.write_file",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "path": "docs/example.md",
                        "content": "Example",
                        "message": "docs: add example",
                        "branch": "feature/agent-runtime-test",
                    },
                    "purpose": "Write the requested change",
                },
                {
                    "tool": "github.get_ci_status",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "commit_sha": "$latest_write_commit",
                    },
                    "purpose": "Verify CI on the latest commit",
                },
                {
                    "tool": "github.create_pull_request",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "title": "Example change",
                        "body": "Summary and test evidence",
                        "head": "feature/agent-runtime-test",
                        "base": "main",
                    },
                    "purpose": "Open a draft pull request",
                },
            ],
            "expected_outcome": "A reviewed draft pull request with passing checks",
        }
    )


def test_task_requires_safe_feature_branch():
    with pytest.raises(ValidationError):
        make_task(work_branch="main")
    with pytest.raises(ValidationError):
        make_task(work_branch="feature/../main")


def test_task_requires_branch_separation():
    with pytest.raises(ValidationError):
        make_task(base_branch="main", work_branch="main")


def test_state_machine_rejects_invalid_transition():
    with pytest.raises(ValueError):
        validate_transition(TaskStatus.QUEUED, TaskStatus.SUCCEEDED)


def test_state_machine_allows_retry_from_failed():
    validate_transition(TaskStatus.FAILED, TaskStatus.QUEUED)


def test_policy_requires_approval_then_honors_it():
    policy = AutonomyPolicy()
    tool = "github.merge_pull_request"
    assert not policy.evaluate(tool, set(), set()).allowed
    assert not policy.evaluate(tool, {tool}, set()).allowed
    assert policy.evaluate(tool, {tool}, set()).requires_approval
    decision = policy.evaluate(tool, {tool}, {tool})
    assert decision.allowed
    assert not decision.requires_approval


def test_policy_never_allows_prohibited_tools():
    policy = AutonomyPolicy()
    decision = policy.evaluate("audit.disable", {"audit.disable"}, {"audit.disable"})
    assert not decision.allowed
    assert not decision.requires_approval


def test_plan_schema_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AgentPlan.model_validate(
            {
                "steps": [
                    {
                        "tool": "x",
                        "arguments": {},
                        "purpose": "x",
                        "extra": 1,
                    }
                ]
            }
        )


def test_orchestrator_accepts_task_scoped_plan(registry):
    orchestrator = HierarchicalOrchestrator(FakeProvider(), registry)
    orchestrator.validate_plan(valid_plan(), make_task().model_dump())


def test_orchestrator_rejects_repository_escape(registry):
    orchestrator = HierarchicalOrchestrator(FakeProvider(), registry)
    plan = valid_plan().model_dump()
    plan["steps"][0]["arguments"]["repository"] = "attacker/other"
    with pytest.raises(PlanRejected):
        orchestrator.validate_plan(AgentPlan.model_validate(plan), make_task().model_dump())


def test_orchestrator_rejects_write_before_branch(registry):
    orchestrator = HierarchicalOrchestrator(FakeProvider(), registry)
    plan = valid_plan().model_dump()
    plan["steps"][1], plan["steps"][2] = plan["steps"][2], plan["steps"][1]
    with pytest.raises(PlanRejected):
        orchestrator.validate_plan(AgentPlan.model_validate(plan), make_task().model_dump())


def test_tool_registry_rejects_unknown_tools():
    registry = ToolRegistry()
    with pytest.raises(ToolError):
        import asyncio

        asyncio.run(registry.execute("shell.execute", {}))


@pytest.mark.asyncio
async def test_submit_is_idempotent_and_tenant_scoped(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task = make_task().model_dump()
    first = await runtime.submit("tenant-a", "request-0001", task)
    repeated = await runtime.submit("tenant-a", "request-0001", task)
    other_tenant = await runtime.submit("tenant-b", "request-0001", task)
    assert first == repeated
    assert first != other_tenant
    assert await runtime.get_task("tenant-b", first) is None
    assert await runtime.get_task("tenant-a", first) is not None


@pytest.mark.asyncio
async def test_memory_store_is_tenant_scoped(database):
    memory = MemoryStore(database)
    await memory.put("tenant-a", "project", "architecture", "Hierarchical supervisor")
    assert await memory.get("tenant-a", "project", "architecture") is not None
    assert await memory.get("tenant-b", "project", "architecture") is None
    assert len(await memory.search("tenant-a", "supervisor")) == 1
    assert await memory.search("tenant-b", "supervisor") == []
    assert await memory.delete("tenant-a", "project", "architecture")
    assert await memory.get("tenant-a", "project", "architecture") is None


@pytest.mark.asyncio
async def test_message_bus_is_tenant_scoped(database):
    bus = AgentMessageBus(database)
    message = WorkerMessage(
        task_id="task-1",
        correlation_id="task-1",
        from_agent=WorkerRole.SUPERVISOR,
        to_agent=WorkerRole.ANALYST,
        message_type="task.delegate",
        payload={"objective": "inspect"},
    )
    await bus.publish("tenant-a", message)
    assert len(await bus.history("tenant-a", "task-1")) == 1
    assert await bus.history("tenant-b", "task-1") == []


@pytest.mark.asyncio
async def test_expired_worker_lease_is_recovered(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit("tenant-a", "request-0002", make_task().model_dump())
    async with database.session() as session:
        row = await session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        row.status = TaskStatus.RUNNING.value
        row.attempts = 1
        row.lease_owner = "dead-worker"
        row.lease_until = utcnow().replace(year=2000)
        await session.commit()
    recovered = await runtime.recover_expired_leases()
    task = await runtime.get_task("tenant-a", task_id)
    assert recovered == 1
    assert task["status"] == TaskStatus.QUEUED.value
    assert task["error"] == "Worker lease expired; task recovered by scheduler"


@pytest.mark.asyncio
async def test_claim_moves_queued_task_to_running(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit("tenant-a", "request-0003", make_task().model_dump())
    claimed = await runtime.claim()
    task = await runtime.get_task("tenant-a", task_id)
    assert claimed == task_id
    assert task["status"] == TaskStatus.RUNNING.value
    assert task["attempts"] == 1
