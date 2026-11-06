import asyncio

from rusa_agents.audit import AuditLog
from rusa_agents.contracts import CodingTask, RepositoryTarget, WorkerResult, WorkerRole, TaskState
from rusa_agents.memory import SQLiteMemory
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.policy import AutonomyPolicy, RiskLevel
from rusa_agents.workers import Worker


class StubWorker:
    def __init__(self, role, evidence=None):
        self.role = role
        self.evidence = evidence or {}

    async def run(self, task, prior_results):
        return WorkerResult(role=self.role, summary="ok", success=True, evidence=self.evidence)


def task():
    return CodingTask(
        objective="Implement AICode",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/test"),
        acceptance_criteria=["Tests pass", "Security review passes"],
    )


def workers(with_evidence=True):
    evidence = {"test_run": "pytest"} if with_evidence else {}
    security = {"review": "passed"} if with_evidence else {}
    return [
        StubWorker(WorkerRole.ANALYST),
        StubWorker(WorkerRole.ARCHITECT),
        StubWorker(WorkerRole.DEVELOPER),
        StubWorker(WorkerRole.DEBUGGER),
        StubWorker(WorkerRole.TESTER, evidence),
        StubWorker(WorkerRole.SECURITY, security),
    ]


def test_policy_protects_production_actions():
    policy = AutonomyPolicy()
    assert policy.evaluate("workspace_write").allowed
    assert policy.evaluate("deploy").requires_approval
    assert not policy.evaluate("disable_audit").allowed
    assert policy.evaluate("repo_read", RiskLevel.CRITICAL).requires_approval


def test_memory_persists_task(tmp_path):
    memory = SQLiteMemory(str(tmp_path / "memory.sqlite3"))
    memory.save("task-1", "StrategyG/RusaAi", "executing", {"attempt": 2})
    restored = SQLiteMemory(str(tmp_path / "memory.sqlite3")).get("task-1")
    assert restored["payload"]["attempt"] == 2
    assert restored["state"] == "executing"


def test_audit_appends_events(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    audit.record("started", "task-1", "master")
    audit.record("finished", "task-1", "master")
    assert len((tmp_path / "audit.jsonl").read_text().splitlines()) == 2


def test_orchestrator_requires_evidence_before_success(tmp_path):
    orchestrator = AICodeOrchestrator(
        workers(), memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.SUCCEEDED


def test_orchestrator_does_not_certify_missing_evidence(tmp_path):
    orchestrator = AICodeOrchestrator(
        workers(False), memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL
