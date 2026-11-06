import asyncio
import pytest
from rusa_agents.audit import AuditLog, AuditIntegrityError
from rusa_agents.contracts import (CodingTask, RepositoryTarget, Evidence, EvidenceKind,
                                   WorkerResult, WorkerRole, TaskState)
from rusa_agents.evidence import RejectUnverifiedEvidence
from rusa_agents.memory import SQLiteMemory, ConcurrentTaskUpdate
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.policy import AutonomyPolicy, RiskLevel


def task(tenant="tenant-a"):
    return CodingTask("Implement feature", RepositoryTarget("StrategyG/RusaAi", "main",
                       "feature/test-task"), ["tests pass"], tenant)


def test_policy_empty_allowlist_fails_closed():
    assert not AutonomyPolicy(set()).evaluate("repo_read").allowed
    assert AutonomyPolicy().evaluate("deploy").requires_approval
    assert not AutonomyPolicy().evaluate("disable_audit").allowed
    assert AutonomyPolicy().evaluate("repo_read", RiskLevel.CRITICAL).requires_approval


def test_memory_is_tenant_scoped_and_versioned(tmp_path):
    memory = SQLiteMemory(str(tmp_path / "memory.db"))
    assert memory.save("a", "task", "owner/repo", "running", {"x": 1}) == 1
    assert memory.get("b", "task") is None
    assert memory.get("a", "task")["payload"]["x"] == 1
    with pytest.raises(ConcurrentTaskUpdate):
        memory.save("a", "task", "owner/repo", "done", {}, expected_version=0)


def test_audit_integrity_and_redaction(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(str(path), "k" * 32)
    audit.record("started", "task", "master", {"api_key": "do-not-log"})
    assert audit.verify()
    assert "[REDACTED]" in path.read_text()
    path.write_text(path.read_text().replace("started", "tampered"))
    with pytest.raises(AuditIntegrityError):
        audit.verify()


def test_invalid_repository_branch_is_rejected():
    with pytest.raises(ValueError):
        RepositoryTarget("owner/repo", "main", "main")


def test_default_evidence_verifier_rejects_claims():
    result = asyncio.run(RejectUnverifiedEvidence().verify(
        Evidence(EvidenceKind.TEST_RUN, "123", "a" * 40), task()))
    assert result is False


class StubWorker:
    def __init__(self, role, blockers=None):
        self.role, self.blockers = role, blockers or []
    async def run(self, task, prior_results):
        return WorkerResult(self.role, "report", True, blockers=self.blockers)


def test_orchestrator_stops_on_worker_blocker(tmp_path):
    workers = [StubWorker(role, ["blocked"] if role == WorkerRole.DEVELOPER else [])
               for role in AICodeOrchestrator.PIPELINE]
    orchestrator = AICodeOrchestrator(
        workers, SQLiteMemory(str(tmp_path / "db.sqlite")),
        AuditLog(str(tmp_path / "audit.jsonl"), "k" * 32))
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.BLOCKED
    assert not any(x.role == WorkerRole.TESTER for x in record.results)
