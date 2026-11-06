from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
import pytest_asyncio

from rusa_agents.contracts import TaskCreate
from rusa_agents.storage import Database, utcnow
from rusa_agents.worker import AgentRuntime

from rusa_agents.contracts import TaskStatus
from rusa_agents.policy import AutonomyPolicy
from rusa_agents.storage import TaskRow
from rusa_agents.tools import ToolRegistry


class FakeProvider:
    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        return '{"steps": [], "expected_outcome": "benchmark"}'


async def no_op_tool(**kwargs):
    return {"ok": True}


def make_task():
    return TaskCreate.model_validate({
        "objective": "Exercise deterministic benchmark scenario",
        "repository": "StrategyG/RusaAi",
        "work_branch": "feature/benchmark-contract-test",
        "acceptance_criteria": ["Required evidence is present"],
    })


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'benchmark-test.db'}")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def registry():
    tools = ToolRegistry()
    tools.register("benchmark.noop", no_op_tool)
    return tools


BENCHMARK = json.loads(
    (Path(__file__).resolve().parents[1] / "benchmarks" / "scenarios.json").read_text()
)


def test_benchmark_manifest_has_required_scenario_classes():
    classes = {case["class"] for case in BENCHMARK["cases"]}
    assert classes == {
        "normal",
        "policy_rejection",
        "tool_failure",
        "qa_failure",
        "worker_recovery",
    }
    assert len(BENCHMARK["cases"]) == 5
    assert all(case["sample_size"] >= 1 for case in BENCHMARK["cases"])


def test_policy_benchmark_rejects_prohibited_action_even_with_grants():
    policy = AutonomyPolicy()
    decision = policy.evaluate(
        "audit.disable",
        {"audit.disable"},
        {"audit.disable"},
    )
    assert decision.allowed is False
    assert decision.requires_approval is False


def test_policy_benchmark_distinguishes_approval_from_denial():
    policy = AutonomyPolicy()
    protected = "github.merge_pull_request"
    pending = policy.evaluate(protected, {protected}, set())
    approved = policy.evaluate(protected, {protected}, {protected})
    unlisted = policy.evaluate("github.write_file", set(), set())

    assert (pending.allowed, pending.requires_approval) == (False, True)
    assert (approved.allowed, approved.requires_approval) == (True, False)
    assert (unlisted.allowed, unlisted.requires_approval) == (False, False)


@pytest.mark.asyncio
async def test_tool_failure_is_not_silently_accepted():
    async def broken_tool(**kwargs):
        raise RuntimeError("simulated upstream timeout")

    registry = ToolRegistry()
    registry.register("benchmark.flaky", broken_tool)
    with pytest.raises(RuntimeError, match="simulated upstream timeout"):
        await registry.execute("benchmark.flaky", {})


@pytest.mark.asyncio
async def test_qa_failure_cannot_be_marked_succeeded(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit(
        "tenant-benchmark",
        "qa-failure-001",
        make_task().model_dump(),
    )
    async with database.session() as session:
        row = await session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        row.status = TaskStatus.RUNNING.value
        row.lease_owner = runtime.worker_id
        row.lease_until = row.created_at
        row.tool_results = []
        await session.commit()

    await runtime._verify_and_finish(
        "tenant-benchmark",
        task_id,
        {"summary": "synthetic QA failure", "citations": []},
    )
    task = await runtime.get_task("tenant-benchmark", task_id)
    assert task["status"] == TaskStatus.BLOCKED.value
    assert task["error"] == "No feature branch creation evidence"


@pytest.mark.asyncio
async def test_worker_crash_recovery_is_deterministic(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit(
        "tenant-benchmark",
        "worker-crash-001",
        make_task().model_dump(),
    )
    async with database.session() as session:
        row = await session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        row.status = TaskStatus.RUNNING.value
        row.attempts = 1
        row.lease_owner = "terminated-worker"
        row.lease_until = utcnow().replace(year=2000)
        await session.commit()

    assert await runtime.recover_expired_leases() == 1
    recovered = await runtime.get_task("tenant-benchmark", task_id)
    assert recovered["status"] == TaskStatus.QUEUED.value
    assert recovered["attempts"] == 1
