"""Small, synthetic benchmark for the staging agent runtime."""

import argparse
import asyncio
import hashlib
import json
import math
import multiprocessing
import os
import subprocess
from collections import deque
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import psycopg

import rusa_agents.policy as policy_module
from rusa_agents.contracts import TaskState, ToolCall, ToolResult, WorkerRole
from rusa_agents.memory import PostgresMemoryStore
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.provider import OpenAICompatibleProvider
from rusa_agents.queue import PostgresTaskQueue
from rusa_agents.worker import QueueWorker
from rusa_agents.workers import AgentWorker
from test_agent_runtime import (
    FakeAudit,
    FakeTools,
    ScriptedWorker,
    make_orchestrator,
    make_task,
    worker_result,
)


class UnavailableTool(FakeTools):
    async def execute(self, task, call):
        self.calls.append((call.name, dict(call.arguments)))
        return ToolResult(name=call.name, success=False, data={}, evidence={})


class FailingCI(FakeTools):
    async def execute(self, task, call):
        if call.name == "get_ci_status":
            self.ci_outcomes = deque(["failed"])
        return await super().execute(task, call)


class CrashAfterCheckpoint(PostgresTaskQueue):
    def checkpoint(self, tenant_id, task_id, worker_id, lease_token, record):
        saved = super().checkpoint(tenant_id, task_id, worker_id, lease_token, record)
        if saved and record.stage_index == 0 and record.worker_results:
            os._exit(42)
        return saved


def crash_worker(dsn):
    worker = QueueWorker(
        queue=CrashAfterCheckpoint(dsn),
        orchestrator=make_orchestrator(FakeTools()),
        memory=PostgresMemoryStore(dsn),
        worker_id="benchmark-crash",
        lease_seconds=60,
    )
    asyncio.run(worker.run_once())


def recover_worker(dsn):
    queue = PostgresTaskQueue(dsn)
    memory = PostgresMemoryStore(dsn)
    queue.initialize()
    memory.initialize()
    original = make_task()
    tenant = f"rusa-test-bench-{uuid4().hex}"
    branch = original.expected_work_branch_for(tenant, original.idempotency_key, original.task_id)
    task = replace(original, tenant_id=tenant, repository=replace(original.repository, work_branch=branch))
    start = perf_counter()
    queue.enqueue(task, max_attempts=2)
    try:
        child = multiprocessing.get_context("spawn").Process(target=crash_worker, args=(dsn,))
        child.start()
        child.join(timeout=20)
        if child.is_alive():
            child.kill()
            child.join()
        assert child.exitcode == 42, "worker did not die at the committed checkpoint"
        crashed = queue.get(tenant, task.task_id)
        assert crashed["status"] == "running" and crashed["attempts"] == 1
        assert crashed["result"]["stage_index"] == 0
        assert len(crashed["result"]["worker_results"]) == 1
        with psycopg.connect(dsn) as conn:
            updated = conn.execute(
                "UPDATE rusa_agent_tasks SET lease_until=now()-interval '1 second' "
                "WHERE tenant_id=%s AND task_id=%s AND status='running'",
                (tenant, task.task_id),
            )
            assert updated.rowcount == 1

        analyst = ScriptedWorker(WorkerRole.ANALYST, [])
        worker = QueueWorker(
            queue=queue,
            orchestrator=make_orchestrator(FakeTools(), {WorkerRole.ANALYST: analyst}),
            memory=memory,
            worker_id="benchmark-replacement",
            lease_seconds=60,
        )
        assert asyncio.run(worker.run_once())
        saved = queue.get(tenant, task.task_id)
        record = saved["result"]
        passed = (
            saved["status"] == "succeeded"
            and record["stage_index"] == 7
            and saved["attempts"] == 2
            and analyst.calls == 0
        )
        return sample(
            "worker_crash", saved["status"], passed, start,
            expected_success=True, repairs=record["repair_cycles"], reclaims=saved["attempts"] - 1,
        )
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM rusa_agent_tasks WHERE tenant_id=%s", (tenant,))
        memory.delete_tenant(tenant)

def sample(case, state, passed, start, *, expected_success, repairs, reclaims=None):
    return {
        "case": case,
        "state": state,
        "expected_outcome_met": passed,
        "expected_success": expected_success,
        "latency_ms": round((perf_counter() - start) * 1000, 3),
        "repair_cycles": repairs,
        "queue_retries": reclaims,
    }


def deterministic_cases():
    developer = WorkerRole.DEVELOPER
    deny_worker = ScriptedWorker(developer, [worker_result(
        developer, [ToolCall("write_file", {"path": ".env", "content": "synthetic"})],
    )])
    for case, gateway, overrides, expected, error, repairs in (
        ("normal", FakeTools(), {}, TaskState.SUCCEEDED, "", 0),
        ("policy_block", FakeTools(), {developer: deny_worker}, TaskState.AWAITING_APPROVAL,
         "protected or unsafe", 0),
        ("tool_failure", UnavailableTool(), {}, TaskState.BLOCKED, "unsuccessful result", 0),
        ("qa_failure", FailingCI(), {}, TaskState.BLOCKED, "maximum repair cycles", 2),
    ):
        start = perf_counter()
        record = asyncio.run(make_orchestrator(gateway, overrides).run(make_task()))
        passed = (
            record.state == expected
            and record.repair_cycles == repairs
            and (not error or any(error in item for item in record.errors))
        )
        if case == "policy_block":
            passed &= not gateway.calls
        yield sample(
            case, record.state.value, passed, start,
            expected_success=expected == TaskState.SUCCEEDED, repairs=record.repair_cycles,
        )


def policy_cases():
    policy = policy_module.AutonomyPolicy()
    repository = make_task().repository
    cases = (
        ("repo_write", "rusa_agents/example.py", (True, False)),
        ("repo_write", ".env", (False, True)),
        ("merge", None, (False, True)),
        ("read_secret", None, (False, False)),
        ("unknown", None, (False, False)),
    )
    return [
        (decision.allowed, decision.requires_approval) == expected
        for action, path, expected in cases
        for decision in [policy.evaluate(action, repository=repository, path=path)]
    ]


def model_case(provider):
    tools = FakeTools()
    orchestrator = AICodeOrchestrator(
        workers=[AgentWorker(role, provider, enabled_tools={
            "create_pull_request", "get_ci_status", "get_branch_diff",
        }) for role in WorkerRole],
        tools=tools,
        audit=FakeAudit(),
    )
    task = replace(make_task(), objective=(
        "Verify a synthetic repository change. The developer must request create_pull_request "
        "with title 'Benchmark'; the tester must request get_ci_status; security must request "
        "get_ci_status and get_branch_diff, then complete a tool-free review. "
        "Only these three tools are available. Verify both acceptance criteria from evidence."
    ))
    start = perf_counter()
    record = asyncio.run(orchestrator.run(task))
    return sample(
        "normal", record.state.value, record.state == TaskState.SUCCEEDED, start,
        expected_success=True, repairs=record.repair_cycles,
    )


def rate(matches, n):
    return {"value": matches / n if n else None, "matches": matches, "n": n}


def summarize(rows, policy, *, include_recovery_latency=False):
    eligible = [row for row in rows if row["expected_success"]]
    succeeded = [row for row in eligible if row["expected_outcome_met"]]
    by_case = {}
    for case in sorted({row["case"] for row in rows}):
        group = [row for row in rows if row["case"] == case]
        latencies = sorted(row["latency_ms"] for row in group)
        by_case[case] = {
            "n": len(group),
            "p95_ms": latencies[math.ceil(0.95 * len(group)) - 1]
            if case != "worker_crash" or include_recovery_latency else None,
            "repair_cycles": sum(row["repair_cycles"] for row in group),
        }
    queued = [row["queue_retries"] for row in rows if row["queue_retries"] is not None]
    return {
        "scenario_conformance": rate(sum(row["expected_outcome_met"] for row in rows), len(rows)),
        "task_success": rate(len(succeeded), len(eligible)),
        "first_attempt_success": rate(sum(
            row["repair_cycles"] == 0 and not row["queue_retries"] for row in succeeded
        ), len(eligible)),
        "policy_accuracy": rate(sum(policy), len(policy)),
        "queue_retries": {"total": sum(queued) if queued else None, "n": len(queued)},
        "cost_per_task": {
            "value": None, "n": 0,
            "reason": "scripted fixtures or provider adapter without usage and billing data",
        },
        "by_case": by_case,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="store_true", help="call a configured real model")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--require-postgres", action="store_true")
    args = parser.parse_args()
    if args.repeats < (3 if args.model else 1):
        parser.error("use at least 3 repetitions for a real model and 1 for scripted fixtures")
    root = Path(__file__).resolve().parents[1]
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip():
        parser.error("benchmark requires a clean checkout for SHA provenance")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dsn = os.getenv("RUSA_BENCH_DATABASE_URL")
    if args.require_postgres and (not dsn or args.model):
        parser.error("--require-postgres requires a dedicated RUSA_BENCH_DATABASE_URL in scripted mode")
    policy = []
    missing = {}
    if args.model:
        model = os.environ["RUSA_BENCH_MODEL_VERSION"]
        provider = OpenAICompatibleProvider(
            model=model,
            base_url=os.environ["RUSA_BENCH_MODEL_BASE_URL"],
            api_key=os.environ["RUSA_BENCH_MODEL_API_KEY"],
        )
        rows = [model_case(provider) for _ in range(args.repeats)]
        missing["model_version"] = "requested pinned identifier; provider response version not exposed"
        missing["policy_and_fault_scenarios"] = "covered by scripted mode"
    else:
        model = "scripted-workers/v1"
        rows = []
        for _ in range(args.repeats):
            rows.extend(deterministic_cases())
            policy.extend(policy_cases())
            if dsn:
                rows.append(recover_worker(dsn))
        if not dsn:
            missing["worker_crash"] = "dedicated PostgreSQL URL not configured"
        else:
            missing["worker_crash_p95"] = "lease expiration forced; natural recovery latency unmeasured"
    print(json.dumps({
        "dataset": "synthetic-agent-runtime/v1",
        "git_sha": sha,
        "model_version": model,
        "policy_version": hashlib.sha256(Path(policy_module.__file__).read_bytes()).hexdigest(),
        "repeats": args.repeats,
        "missing": missing,
        "metrics": summarize(rows, policy),
        "samples": rows,
    }, indent=2))
    return int(not args.model and not (
        all(row["expected_outcome_met"] for row in rows) and all(policy)
    ))


if __name__ == "__main__":
    raise SystemExit(main())
