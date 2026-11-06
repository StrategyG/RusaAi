from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from datetime import timedelta, timezone, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from rusa_agents.audit import AuditLog
from rusa_agents.contracts import TaskCreate, TaskStatus
from rusa_agents.evaluation import AgentEvaluator
from rusa_agents.policy import AutonomyPolicy, POLICY_VERSION
from rusa_agents.storage import Database, TaskRow, utcnow
from rusa_agents.tools import ToolRegistry
from rusa_agents.worker import AgentRuntime


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "scenarios.json"
TENANT_ID = "benchmark-tenant"


class ScenarioProvider:
    """Deterministic provider fixture; it never calls an external model."""

    def __init__(self, scenario: str):
        self.scenario = scenario

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        if "independent Reviewer agent" in system_prompt:
            return json.dumps({
                "approved": True,
                "summary": "Deterministic benchmark review approved",
                "findings": [],
                "risk_level": "low",
            })

        if "Synthesis agent" in system_prompt:
            task = json.loads(user_prompt)
            return json.dumps({
                "summary": "Deterministic benchmark task completed",
                "key_findings": ["Synthetic tool evidence was evaluated"],
                "citations": [],
                "acceptance_criteria_assessment": [
                    {
                        "criterion": criterion,
                        "status": "met",
                        "evidence": "The deterministic scenario reached its expected state",
                    }
                    for criterion in task["acceptance_criteria"]
                ],
            })

        task = json.loads(user_prompt)
        repository = task["repository"]
        base = task["base_branch"]
        work = task["work_branch"]
        steps: list[dict[str, Any]] = [
            {
                "tool": "github.list_files",
                "arguments": {"repository": repository, "path": "", "ref": base},
                "purpose": "Inspect the benchmark repository",
            },
            {
                "tool": "github.create_branch",
                "arguments": {
                    "repository": repository,
                    "base_branch": base,
                    "work_branch": work,
                },
                "purpose": "Create an isolated benchmark branch",
            },
        ]

        if self.scenario == "policy_rejection":
            steps.append({
                "tool": "audit.disable",
                "arguments": {},
                "purpose": "Exercise a prohibited policy action",
            })
        elif self.scenario == "tool_failure":
            steps.append({
                "tool": "github.read_file",
                "arguments": {
                    "repository": repository,
                    "path": "README.md",
                    "ref": base,
                },
                "purpose": "Exercise a failing external tool",
            })
        elif self.scenario == "qa_failure":
            steps.extend([
                {
                    "tool": "github.write_file",
                    "arguments": {
                        "repository": repository,
                        "path": "docs/benchmark.md",
                        "content": "deterministic QA fixture",
                        "message": "test: add deterministic QA fixture",
                        "branch": work,
                    },
                    "purpose": "Create a controlled test change",
                },
                {
                    "tool": "github.compare_branches",
                    "arguments": {
                        "repository": repository,
                        "base": base,
                        "head": work,
                    },
                    "purpose": "Collect the branch diff",
                },
                {
                    "tool": "agent.review_diff",
                    "arguments": {},
                    "purpose": "Review the test change",
                },
                {
                    "tool": "github.create_pull_request",
                    "arguments": {
                        "repository": repository,
                        "title": "Deterministic benchmark fixture",
                        "body": "Synthetic QA benchmark",
                        "head": work,
                        "base": base,
                    },
                    "purpose": "Create a draft pull request fixture",
                },
                {
                    "tool": "github.get_ci_status",
                    "arguments": {
                        "repository": repository,
                        "commit_sha": "$latest_write_commit",
                    },
                    "purpose": "Verify the latest write commit",
                },
            ])

        return json.dumps({
            "steps": steps,
            "expected_outcome": f"Exercise {self.scenario} lifecycle behavior",
        })


async def _ok_list_files(**kwargs: Any) -> dict[str, Any]:
    return {"entries": [{"name": "README.md", "type": "file"}]}


async def _ok_create_branch(**kwargs: Any) -> dict[str, Any]:
    return {"branch": kwargs["work_branch"], "sha": "a" * 40}


async def _failing_read_file(**kwargs: Any) -> dict[str, Any]:
    raise RuntimeError("simulated benchmark tool timeout")


async def _ok_write_file(**kwargs: Any) -> dict[str, Any]:
    return {"path": kwargs["path"], "commit_sha": "b" * 40}


async def _ok_compare_branches(**kwargs: Any) -> dict[str, Any]:
    return {
        "files": [{"filename": "docs/benchmark.md", "patch": "+deterministic QA fixture"}],
        "ahead_by": 1,
    }


async def _ok_create_pr(**kwargs: Any) -> dict[str, Any]:
    return {"number": 1, "draft": True}


async def _failed_ci(**kwargs: Any) -> dict[str, Any]:
    return {
        "commit_sha": kwargs["commit_sha"],
        "all_passed": False,
        "checks": [{"name": "synthetic-qa", "conclusion": "failure"}],
    }


async def _prohibited_tool(**kwargs: Any) -> dict[str, Any]:
    return {"unexpected_execution": True}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    schema = {"type": "object", "additionalProperties": True}
    handlers = {
        "github.list_files": _ok_list_files,
        "github.create_branch": _ok_create_branch,
        "github.read_file": _failing_read_file,
        "github.write_file": _ok_write_file,
        "github.compare_branches": _ok_compare_branches,
        "github.create_pull_request": _ok_create_pr,
        "github.get_ci_status": _failed_ci,
        "audit.disable": _prohibited_tool,
    }
    for name, handler in handlers.items():
        registry.register(name, handler, input_schema=schema)
    return registry


def _allowed_tools(scenario: str) -> list[str]:
    common = ["github.list_files", "github.create_branch"]
    if scenario == "policy_rejection":
        return [*common, "audit.disable"]
    if scenario == "tool_failure":
        return [*common, "github.read_file"]
    if scenario == "qa_failure":
        return [
            *common,
            "github.write_file",
            "github.compare_branches",
            "agent.review_diff",
            "github.create_pull_request",
            "github.get_ci_status",
        ]
    return common


def _task(scenario: str) -> TaskCreate:
    return TaskCreate.model_validate({
        "objective": f"Run the {scenario} deterministic benchmark scenario",
        "repository": "benchmark/fixture-repository",
        "base_branch": "main",
        "work_branch": f"chore/benchmark-{scenario}",
        "context": f"scenario={scenario}",
        "acceptance_criteria": [f"Scenario {scenario} reaches its expected state"],
        "allowed_tools": _allowed_tools(scenario),
        "max_attempts": 2 if scenario in {"tool_failure", "worker_recovery"} else 1,
        "require_ci": True,
        "require_pull_request": True,
    })


async def _record_policy_labels(database: Database, policy_cases: list[dict[str, Any]]) -> dict[str, Any]:
    policy = AutonomyPolicy()
    audit = AuditLog(database)
    correct = false_allow = false_deny = 0
    for case in policy_cases:
        decision = policy.evaluate(
            case["tool"],
            set(case["allowed_tools"]),
            set(case["approved_actions"]),
        )
        expected_allowed = case["expected_allowed"]
        expected_approval = case["expected_requires_approval"]
        if (
            decision.allowed == expected_allowed
            and decision.requires_approval == expected_approval
        ):
            correct += 1
        if not expected_allowed and decision.allowed:
            false_allow += 1
        if expected_allowed and not decision.allowed:
            false_deny += 1
        await audit.record(
            TENANT_ID,
            f"policy-case-{case['id']}",
            "benchmark",
            "policy.benchmark_decision",
            {
                "case_id": case["id"],
                "tool": case["tool"],
                "expected_allowed": expected_allowed,
                "actual_allowed": decision.allowed,
                "expected_requires_approval": expected_approval,
                "actual_requires_approval": decision.requires_approval,
                "policy_version": POLICY_VERSION,
            },
        )
    sample_size = len(policy_cases)
    return {
        "sample_size": sample_size,
        "accuracy": correct / sample_size if sample_size else None,
        "correct": correct,
        "false_allow_count": false_allow if sample_size else None,
        "false_deny_count": false_deny if sample_size else None,
    }


async def run(output: Path) -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    scenarios = manifest["cases"]
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rusa-agent-benchmark-") as directory:
        database = Database(f"sqlite+aiosqlite:///{Path(directory) / 'benchmark.db'}")
        await database.initialize()
        results: list[dict[str, Any]] = []
        try:
            for scenario in scenarios:
                scenario_id = scenario["class"]
                runtime = AgentRuntime(
                    database,
                    ScenarioProvider(scenario_id),
                    _registry(),
                    lease_seconds=60,
                    heartbeat_seconds=5,
                )
                task_id = await runtime.submit(
                    TENANT_ID,
                    f"benchmark-{scenario_id}",
                    _task(scenario_id).model_dump(),
                )

                if scenario_id == "worker_recovery":
                    async with database.session() as session:
                        row = await session.scalar(
                            select(TaskRow).where(TaskRow.id == task_id)
                        )
                        row.status = TaskStatus.RUNNING.value
                        row.attempts = 1
                        row.lease_owner = "terminated-benchmark-worker"
                        row.lease_until = utcnow() - timedelta(seconds=1)
                        await session.commit()
                    recovered = await runtime.recover_expired_leases()
                    task = await runtime.get_task(TENANT_ID, task_id)
                    actual_status = task["status"]
                    scenario_passed = (
                        recovered == 1 and actual_status == TaskStatus.QUEUED.value
                    )
                else:
                    await runtime.run_once()
                    task = await runtime.get_task(TENANT_ID, task_id)
                    if scenario_id == "tool_failure":
                        async with database.session() as session:
                            row = await session.scalar(
                                select(TaskRow).where(TaskRow.id == task_id)
                            )
                            row.available_at = utcnow()
                            await session.commit()
                        await runtime.run_once()
                        task = await runtime.get_task(TENANT_ID, task_id)
                    actual_status = task["status"]
                    expected_status = {
                        "normal": TaskStatus.SUCCEEDED.value,
                        "policy_rejection": TaskStatus.FAILED.value,
                        "tool_failure": TaskStatus.FAILED.value,
                        "qa_failure": TaskStatus.BLOCKED.value,
                    }[scenario_id]
                    scenario_passed = actual_status == expected_status

                results.append({
                    "scenario_id": scenario["id"],
                    "scenario_class": scenario_id,
                    "expected": scenario["expected"],
                    "actual_status": actual_status,
                    "passed": scenario_passed,
                    "task_id": task_id,
                    "attempts": task["attempts"],
                })

            policy_metrics = await _record_policy_labels(
                database,
                manifest.get("policy_cases", []),
            )
            runtime_metrics = await AgentEvaluator(database).metrics(
                TENANT_ID,
                limit=100,
            )
            scenario_pass_count = sum(1 for item in results if item["passed"])
            scenario_count = len(results)
            try:
                git_sha = os.getenv("RUSA_BENCHMARK_CANDIDATE_SHA") or subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError):
                git_sha = None

            report = {
                "benchmark_id": manifest["benchmark_id"],
                "dataset_version": f"{manifest['benchmark_id']}:{manifest['version']}",
                "candidate_sha": git_sha,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model_name": "deterministic-fixture-provider",
                "model_revision": "not-applicable",
                "policy_version": POLICY_VERSION,
                "sample_size": scenario_count,
                "scenario_counts": {
                    scenario_class: sum(
                        1 for item in results if item["scenario_class"] == scenario_class
                    )
                    for scenario_class in sorted({item["scenario_class"] for item in results})
                },
                "scenario_pass_rate": (
                    scenario_pass_count / scenario_count if scenario_count else None
                ),
                "scenario_pass_count": scenario_pass_count,
                "scenario_results": results,
                "task_success_rate": runtime_metrics["task_success_rate"],
                "first_attempt_success_rate": runtime_metrics["first_attempt_success_rate"],
                "terminal_task_sample_size": runtime_metrics["terminal_sample_size"],
                "policy_decision_accuracy": policy_metrics["accuracy"],
                "policy_decision_sample_size": policy_metrics["sample_size"],
                "false_allow_count": policy_metrics["false_allow_count"],
                "false_deny_count": policy_metrics["false_deny_count"],
                "latency_p95_ms": runtime_metrics["latency_p95_ms"],
                "latency_sample_size": runtime_metrics["latency_sample_size"],
                "latency_measurement_scope": (
                    "In-process SQLite deterministic fixture; not a production SLO baseline"
                ),
                "retry_rate": runtime_metrics["retry_rate"],
                "retry_count": runtime_metrics["retry_count"],
                "cost_per_task": None,
                "cost_measurement_status": runtime_metrics["cost_measurement_status"],
                "unmeasured_metrics": [
                    "cost_per_task",
                    "real_model_quality",
                    "real_provider_latency",
                ],
                "runtime_metrics": runtime_metrics,
                "baseline_status": "DETERMINISTIC_FIXTURE_ONLY",
            }
            if scenario_pass_count != scenario_count:
                raise RuntimeError(
                    f"Deterministic benchmark failed: {scenario_pass_count}/{scenario_count} scenarios passed"
                )
            if policy_metrics["accuracy"] != 1.0 or policy_metrics["false_allow_count"] != 0:
                raise RuntimeError("Policy benchmark failed its labeled decision gate")

            output.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return report
        finally:
            await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic RUSA-AI agent benchmark")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "benchmark-report.json",
    )
    args = parser.parse_args()
    report = asyncio.run(run(args.output))
    print(json.dumps({
        "benchmark_id": report["benchmark_id"],
        "candidate_sha": report["candidate_sha"],
        "sample_size": report["sample_size"],
        "scenario_pass_rate": report["scenario_pass_rate"],
        "policy_decision_accuracy": report["policy_decision_accuracy"],
        "latency_p95_ms": report["latency_p95_ms"],
        "retry_count": report["retry_count"],
        "report_path": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
