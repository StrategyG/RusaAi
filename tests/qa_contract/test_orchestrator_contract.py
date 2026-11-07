"""Immutable QA contract tests for fail-closed orchestrator behavior."""
import asyncio

from rusa_agents.audit import AuditLog
from rusa_agents.contracts import (
    CodingTask,
    RepositoryTarget,
    TaskState,
    WorkerResult,
    WorkerRole,
)
from rusa_agents.memory import SQLiteMemory
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.qa_client import QAAttestationError, QAServiceUnavailable


class Worker:
    def __init__(self, role):
        self.role = role
        if role == WorkerRole.TESTER:
            self.evidence = {"check:pytest": {"profile": "pytest", "exit_code": 0}}
        elif role == WorkerRole.SECURITY:
            self.evidence = {
                "check:bandit": {"profile": "bandit", "exit_code": 0},
                "check:ruff": {"profile": "ruff", "exit_code": 0},
                "check:compile": {"profile": "compile", "exit_code": 0},
            }
        else:
            self.evidence = {}

    async def run(self, task, prior_results):
        return WorkerResult(
            role=self.role,
            summary="worker completed",
            success=True,
            evidence=self.evidence,
        )


def task():
    return CodingTask(
        objective="validate independent QA",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/qa-contract"),
        acceptance_criteria=["independent QA PASS required"],
        task_id="qa-contract-task",
        metadata={"tenant_id": "tenant-a"},
    )


def envelope(verdict="PASS"):
    return {
        "payload": {
            "service_id": "rusa-independent-qa",
            "key_id": "qa-contract-key",
            "verdict": verdict,
            "git_tree_sha": "a" * 40,
            "artifact_digest": "b" * 64,
            "profiles": {
                "pytest": {"exit_code": 0 if verdict == "PASS" else 1},
                "bandit": {"exit_code": 0},
                "ruff": {"exit_code": 0},
                "compile": {"exit_code": 0},
            },
        },
        "signature": "only-used-behind-the-injected-validator-boundary",
    }


class Validator:
    def __init__(self, verdicts=None, error=None):
        self.verdicts = list(verdicts or ["PASS"])
        self.error = error
        self.calls = 0

    async def validate(self, task):
        self.calls += 1
        if self.error:
            raise self.error
        return envelope(self.verdicts[min(self.calls - 1, len(self.verdicts) - 1)])


def build_orchestrator(tmp_path, validator=None):
    return AICodeOrchestrator(
        workers=[Worker(role) for role in AICodeOrchestrator.PIPELINE],
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        qa_validator=validator,
        require_independent_qa=True,
        max_repair_cycles=1,
    )


def test_orchestrator_holds_if_independent_qa_is_not_configured(tmp_path):
    record = asyncio.run(build_orchestrator(tmp_path).run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL


def test_orchestrator_requires_independent_qa_pass(tmp_path):
    validator = Validator()
    record = asyncio.run(build_orchestrator(tmp_path, validator).run(task()))
    assert record.state == TaskState.SUCCEEDED
    assert validator.calls == 1
    assert record.qa_verdict["payload"]["verdict"] == "PASS"


def test_orchestrator_repairs_after_qa_failure(tmp_path):
    validator = Validator(["FAIL", "PASS"])
    record = asyncio.run(build_orchestrator(tmp_path, validator).run(task()))
    assert record.state == TaskState.SUCCEEDED
    assert validator.calls == 2


def test_orchestrator_holds_if_qa_service_is_unavailable(tmp_path):
    validator = Validator(error=QAServiceUnavailable("QA service unavailable"))
    record = asyncio.run(build_orchestrator(tmp_path, validator).run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL


def test_orchestrator_blocks_an_invalid_qa_attestation(tmp_path):
    validator = Validator(error=QAAttestationError("invalid QA signature"))
    record = asyncio.run(build_orchestrator(tmp_path, validator).run(task()))
    assert record.state == TaskState.BLOCKED
