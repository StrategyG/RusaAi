import asyncio

import pytest

from rusa_agents.audit import AuditLog
from rusa_agents.contracts import (
    CodingTask,
    RepositoryTarget,
    TaskState,
    WorkerResult,
    WorkerRole,
)
from rusa_agents.evidence import EvidenceAttestor
from rusa_agents.memory import SQLiteMemory
from rusa_agents.orchestrator import AICodeOrchestrator
from rusa_agents.policy import AutonomyPolicy, RiskLevel

TEST_EVIDENCE_KEY = "qa-test-attestation-key-" + "x" * 32
TEST_TASK_ID = "00000000-0000-4000-8000-000000000001"


def _attestation(
    profile,
    role,
    *,
    task_id=TEST_TASK_ID,
    tenant_id="default",
    exit_code=0,
    snapshot_sha256="a" * 64,
):
    return EvidenceAttestor(TEST_EVIDENCE_KEY).attest(
        profile=profile,
        exit_code=exit_code,
        image="sandbox:test",
        snapshot_sha256=snapshot_sha256,
        task_id=task_id,
        tenant_id=tenant_id,
        worker_role=role.value,
    )


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
        task_id=TEST_TASK_ID,
    )


def workers(with_evidence=True):
    evidence = {"check:pytest": _attestation("pytest", WorkerRole.TESTER)} if with_evidence else {}
    security = {
        "check:bandit": _attestation("bandit", WorkerRole.SECURITY),
        "check:ruff": _attestation("ruff", WorkerRole.SECURITY),
        "check:compile": _attestation("compile", WorkerRole.SECURITY),
    } if with_evidence else {}
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


@pytest.mark.parametrize("action", sorted(AutonomyPolicy.PROTECTED_ACTIONS))
def test_policy_requires_approval_for_every_protected_action(action):
    decision = AutonomyPolicy().evaluate(action)
    assert not decision.allowed
    assert decision.requires_approval


@pytest.mark.parametrize("action", sorted(AutonomyPolicy.DENIED_ACTIONS))
def test_policy_never_allows_prohibited_actions(action):
    decision = AutonomyPolicy().evaluate(action)
    assert not decision.allowed
    assert not decision.requires_approval


def test_memory_persists_task(tmp_path):
    memory = SQLiteMemory(str(tmp_path / "memory.sqlite3"))
    memory.save("task-1", "StrategyG/RusaAi", "executing", {"attempt": 2}, tenant_id="tenant-a")
    memory.save("task-1", "Other/Repo", "planning", {"attempt": 9}, tenant_id="tenant-b")
    restored = SQLiteMemory(str(tmp_path / "memory.sqlite3")).get("task-1", tenant_id="tenant-a")
    isolated = SQLiteMemory(str(tmp_path / "memory.sqlite3")).get("task-1", tenant_id="tenant-b")
    assert restored["payload"]["attempt"] == 2
    assert isolated["payload"]["attempt"] == 9
    assert SQLiteMemory(str(tmp_path / "memory.sqlite3")).get("task-1", tenant_id="tenant-c") is None
    assert restored["state"] == "executing"


def test_audit_appends_events(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    audit.record("started", "task-1", "master")
    audit.record("finished", "task-1", "master")
    assert len((tmp_path / "audit.jsonl").read_text().splitlines()) == 2
    assert audit.verify()
    with (tmp_path / "audit.jsonl").open("a") as stream:
        stream.write('{"tampered": true}\\n')
    assert not audit.verify()


def test_orchestrator_requires_evidence_before_success(tmp_path):
    orchestrator = AICodeOrchestrator(
        workers(), memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.SUCCEEDED


def test_orchestrator_does_not_certify_missing_evidence(tmp_path):
    orchestrator = AICodeOrchestrator(
        workers(False), memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL


def test_orchestrator_blocks_failed_validation_evidence(tmp_path):
    failing = workers()
    failing[-2].evidence = {
        "check:pytest": _attestation("pytest", WorkerRole.TESTER, exit_code=1)
    }
    orchestrator = AICodeOrchestrator(
        failing,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.BLOCKED
    assert any("check:pytest" in error for error in record.errors)



def test_orchestrator_supplies_previous_attempt_memory_to_workers(tmp_path):
    class HistoryWorker(StubWorker):
        def __init__(self, role):
            super().__init__(role)
            self.seen_prior = []

        async def run(self, task, prior_results):
            self.seen_prior = list(prior_results)
            tenant_id = task.metadata.get("tenant_id", "default")
            if self.role == WorkerRole.TESTER:
                self.evidence = {
                    "check:pytest": _attestation(
                        "pytest", self.role, task_id=task.task_id, tenant_id=tenant_id
                    )
                }
            elif self.role == WorkerRole.SECURITY:
                self.evidence = {
                    "check:bandit": _attestation(
                        "bandit", self.role, task_id=task.task_id, tenant_id=tenant_id
                    ),
                    "check:ruff": _attestation(
                        "ruff", self.role, task_id=task.task_id, tenant_id=tenant_id
                    ),
                    "check:compile": _attestation(
                        "compile", self.role, task_id=task.task_id, tenant_id=tenant_id
                    ),
                }
            return await super().run(task, prior_results)

    memory = SQLiteMemory(str(tmp_path / "memory.sqlite3"))
    memory.save(
        "task-history",
        "StrategyG/RusaAi",
        "blocked",
        {
            "results": [{
                "role": "developer",
                "summary": "Changed parser but tests failed",
                "success": True,
                "artifacts": ["src/parser.py"],
                "evidence": {},
                "blockers": [],
            }],
            "errors": ["check:pytest returned exit code 1"],
        },
        tenant_id="tenant-history",
    )
    task_with_history = CodingTask(
        objective="Fix parser tests",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/history"),
        acceptance_criteria=["Parser tests pass"],
        task_id="task-history",
        metadata={"tenant_id": "tenant-history"},
    )
    history_workers = [HistoryWorker(role) for role in AICodeOrchestrator.PIPELINE]
    orchestrator = AICodeOrchestrator(
        history_workers,
        memory=memory,
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task_with_history))
    assert history_workers[0].seen_prior
    assert any("Changed parser" in item.summary for item in history_workers[0].seen_prior)
    assert record.state == TaskState.SUCCEEDED



def test_orchestrator_fails_cleanly_if_memory_lookup_fails(tmp_path):
    class BrokenMemory(SQLiteMemory):
        def get(self, task_id, tenant_id="default"):
            raise RuntimeError("database unavailable")

    orchestrator = AICodeOrchestrator(
        workers(),
        memory=BrokenMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.FAILED
    assert any("database unavailable" in error for error in record.errors)



def test_quality_managers_dispatch_in_parallel(tmp_path):
    started = set()
    barrier = asyncio.Event()

    class ParallelWorker(StubWorker):
        async def run(self, task, prior_results):
            started.add(self.role)
            if started.issuperset({WorkerRole.TESTER, WorkerRole.SECURITY}):
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=1.0)
            return await super().run(task, prior_results)

    evidence_workers = []
    for role in AICodeOrchestrator.PIPELINE:
        if role == WorkerRole.TESTER:
            evidence_workers.append(
                ParallelWorker(
                    role, {"check:pytest": _attestation("pytest", WorkerRole.TESTER)}
                )
            )
        elif role == WorkerRole.SECURITY:
            evidence_workers.append(
                ParallelWorker(
                    role,
                    {
                        "check:bandit": _attestation("bandit", WorkerRole.SECURITY),
                        "check:ruff": _attestation("ruff", WorkerRole.SECURITY),
                        "check:compile": _attestation("compile", WorkerRole.SECURITY),
                    },
                )
            )
        else:
            evidence_workers.append(StubWorker(role))

    orchestrator = AICodeOrchestrator(
        evidence_workers,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
        max_parallel_workers=2,
        max_repair_cycles=0,
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.SUCCEEDED
    assert started == {WorkerRole.TESTER, WorkerRole.SECURITY}


def test_quality_failure_triggers_bounded_repair_cycle(tmp_path):
    class CountingDeveloper(StubWorker):
        def __init__(self):
            super().__init__(WorkerRole.DEVELOPER)
            self.calls = 0

        async def run(self, task, prior_results):
            self.calls += 1
            return WorkerResult(
                role=self.role,
                summary=f"implementation pass {self.calls}",
                success=True,
            )

    class FlakyTester(StubWorker):
        def __init__(self):
            super().__init__(WorkerRole.TESTER)
            self.calls = 0

        async def run(self, task, prior_results):
            self.calls += 1
            return WorkerResult(
                role=self.role,
                summary=f"test pass {self.calls}",
                success=True,
                evidence={
                    "check:pytest": _attestation(
                        "pytest",
                        self.role,
                        task_id=task.task_id,
                        tenant_id=task.metadata.get("tenant_id", "default"),
                        exit_code=1 if self.calls == 1 else 0,
                    )
                },
            )

    class CountingSecurity(StubWorker):
        def __init__(self):
            super().__init__(
                WorkerRole.SECURITY,
                {
                    "check:bandit": _attestation("bandit", WorkerRole.SECURITY),
                    "check:ruff": _attestation("ruff", WorkerRole.SECURITY),
                    "check:compile": _attestation("compile", WorkerRole.SECURITY),
                },
            )
            self.calls = 0

        async def run(self, task, prior_results):
            self.calls += 1
            return await super().run(task, prior_results)

    developer = CountingDeveloper()
    tester = FlakyTester()
    security = CountingSecurity()
    configured = [
        StubWorker(WorkerRole.ANALYST),
        StubWorker(WorkerRole.ARCHITECT),
        developer,
        StubWorker(WorkerRole.DEBUGGER),
        tester,
        security,
    ]
    orchestrator = AICodeOrchestrator(
        configured,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
        max_repair_cycles=1,
    )
    record = asyncio.run(orchestrator.run(task()))

    assert record.state == TaskState.SUCCEEDED
    assert developer.calls == 2
    assert tester.calls == 2
    assert security.calls == 2
    assert len([
        result for result in record.results if result.role == WorkerRole.TESTER
    ]) == 2



def test_orchestrator_rejects_unsigned_worker_claims(tmp_path):
    unsigned = workers()
    unsigned[-2].evidence = {"check:pytest": {"exit_code": 0}}
    orchestrator = AICodeOrchestrator(
        unsigned,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.BLOCKED
    assert any("Invalid or untrusted QA attestations" in error for error in record.errors)


def test_orchestrator_rejects_attestation_from_another_tenant(tmp_path):
    mismatched = workers()
    mismatched[-2].evidence = {
        "check:pytest": _attestation(
            "pytest", WorkerRole.TESTER, tenant_id="tenant-other"
        )
    }
    orchestrator = AICodeOrchestrator(
        mismatched,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.BLOCKED
    assert any("check:pytest" in error for error in record.errors)


def test_orchestrator_rejects_qa_profiles_for_different_snapshots(tmp_path):
    mismatched = workers()
    mismatched[-1].evidence["check:ruff"] = _attestation(
        "ruff", WorkerRole.SECURITY, snapshot_sha256="b" * 64
    )
    orchestrator = AICodeOrchestrator(
        mismatched,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        evidence_key=TEST_EVIDENCE_KEY,
        expected_sandbox_image="sandbox:test",
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.BLOCKED
    assert any("same immutable workspace snapshot" in error for error in record.errors)


def test_orchestrator_holds_when_evidence_verifier_is_not_configured(tmp_path):
    orchestrator = AICodeOrchestrator(
        workers(),
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL
    assert any("verifier is not configured" in error for error in record.errors)
