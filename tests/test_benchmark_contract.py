from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
import pytest_asyncio

from rusa_agents.contracts import TaskCreate
from rusa_agents.storage import Database, utcnow
from rusa_agents.worker import AgentRuntime

from rusa_agents.audit import AuditLog
from rusa_agents.contracts import TaskStatus
from rusa_agents.evaluation import AgentEvaluator, _percentile
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



def test_percentile_uses_nearest_rank_and_handles_empty_samples():
    assert _percentile([], 95) is None
    assert _percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0
    assert _percentile([10.0, 20.0, 30.0, 40.0], 95) == 40.0


@pytest.mark.asyncio
async def test_policy_accuracy_requires_independent_labeled_decisions(database):
    audit = AuditLog(database)
    await audit.record(
        "tenant-policy-metrics",
        "case-allow",
        "benchmark",
        "policy.benchmark_decision",
        {
            "expected_allowed": True,
            "actual_allowed": True,
            "expected_requires_approval": False,
            "actual_requires_approval": False,
        },
    )
    await audit.record(
        "tenant-policy-metrics",
        "case-false-allow",
        "benchmark",
        "policy.benchmark_decision",
        {
            "expected_allowed": False,
            "actual_allowed": True,
            "expected_requires_approval": False,
            "actual_requires_approval": False,
        },
    )
    await audit.record(
        "tenant-policy-metrics",
        "case-runtime",
        "worker-1",
        "policy.decision",
        {
            "tool": "github.write_file",
            "allowed": True,
            "requires_approval": False,
            "policy_version": "rusa-autonomy-policy-v1",
        },
    )

    metrics = await AgentEvaluator(database).metrics("tenant-policy-metrics")
    assert metrics["policy_decision_accuracy"] == 0.5
    assert metrics["policy_decision_sample_size"] == 2
    assert metrics["false_allow_count"] == 1
    assert metrics["false_deny_count"] == 0
    assert metrics["policy_decision_count"] == 1
    assert metrics["policy_version"] == "rusa-autonomy-policy-v1"
    assert metrics["cost_per_task"] is None
    assert "cost_per_task" in metrics["unmeasured_metrics"]


@pytest.mark.asyncio
async def test_policy_metrics_remain_unmeasured_without_labeled_cases(database):
    metrics = await AgentEvaluator(database).metrics("tenant-no-policy-labels")
    assert metrics["policy_decision_accuracy"] is None
    assert metrics["policy_decision_sample_size"] == 0
    assert metrics["false_allow_count"] is None
    assert metrics["false_deny_count"] is None



@pytest.mark.asyncio
async def test_model_provider_captures_usage_when_reported(monkeypatch):
    from rusa_agents.providers import OpenAICompatibleProvider

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "valid response"}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("rusa_agents.providers.httpx.AsyncClient", FakeClient)
    provider = OpenAICompatibleProvider(
        base_url="https://model.example.test/v1",
        model="fixture-model",
    )
    assert await provider.complete("system", "user") == "valid response"
    assert provider.last_usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert provider.usage_totals["total_tokens"] == 18
    assert provider.request_count == 1
    assert provider.usage_calls == 1
    assert provider.usage_missing_calls == 0


@pytest.mark.asyncio
async def test_model_provider_marks_missing_usage_without_fabricating_tokens(monkeypatch):
    from rusa_agents.providers import OpenAICompatibleProvider

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "valid response"}}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("rusa_agents.providers.httpx.AsyncClient", FakeClient)
    provider = OpenAICompatibleProvider(
        base_url="https://model.example.test/v1",
        model="fixture-model",
    )
    assert await provider.complete("system", "user") == "valid response"
    assert provider.last_usage is None
    assert provider.usage_totals["total_tokens"] == 0
    assert provider.request_count == 1
    assert provider.usage_calls == 0
    assert provider.usage_missing_calls == 1
