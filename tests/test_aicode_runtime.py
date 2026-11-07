import asyncio
from types import SimpleNamespace

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
from rusa_agents.policy import AutonomyPolicy, RiskLevel
from rusa_agents.runtime import AICodeRuntime


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
    evidence = {"check:pytest": {"profile": "pytest", "exit_code": 0}} if with_evidence else {}
    security = {
        "check:bandit": {"profile": "bandit", "exit_code": 0},
        "check:ruff": {"profile": "ruff", "exit_code": 0},
        "check:compile": {"profile": "compile", "exit_code": 0},
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


def test_orchestrator_blocks_failed_validation_evidence(tmp_path):
    failing = workers()
    failing[-2].evidence = {"check:pytest": {"profile": "pytest", "exit_code": 1}}
    orchestrator = AICodeOrchestrator(
        failing,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.BLOCKED
    assert any("check:pytest" in error for error in record.errors)



def test_orchestrator_supplies_previous_attempt_memory_to_workers(tmp_path):
    class HistoryWorker(StubWorker):
        def __init__(self, role):
            evidence = {}
            if role == WorkerRole.TESTER:
                evidence = {"check:pytest": {"profile": "pytest", "exit_code": 0}}
            elif role == WorkerRole.SECURITY:
                evidence = {
                    "check:bandit": {"profile": "bandit", "exit_code": 0},
                    "check:ruff": {"profile": "ruff", "exit_code": 0},
                    "check:compile": {"profile": "compile", "exit_code": 0},
                }
            super().__init__(role, evidence)
            self.seen_prior = []

        async def run(self, task, prior_results):
            self.seen_prior = list(prior_results)
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
                ParallelWorker(role, {"check:pytest": {"profile": "pytest", "exit_code": 0}})
            )
        elif role == WorkerRole.SECURITY:
            evidence_workers.append(
                ParallelWorker(
                    role,
                    {
                        "check:bandit": {"profile": "bandit", "exit_code": 0},
                        "check:ruff": {"profile": "ruff", "exit_code": 0},
                        "check:compile": {"profile": "compile", "exit_code": 0},
                    },
                )
            )
        else:
            evidence_workers.append(StubWorker(role))

    orchestrator = AICodeOrchestrator(
        evidence_workers,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
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
                evidence={"check:pytest": {"profile": "pytest", "exit_code": 1 if self.calls == 1 else 0}},
            )

    class CountingSecurity(StubWorker):
        def __init__(self):
            super().__init__(
                WorkerRole.SECURITY,
                {
                    "check:bandit": {"profile": "bandit", "exit_code": 0},
                    "check:ruff": {"profile": "ruff", "exit_code": 0},
                    "check:compile": {"profile": "compile", "exit_code": 0},
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



def test_runtime_task_branch_is_stable_across_lease_retries():
    payload = {
        "objective": "Make a deterministic task branch",
        "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
        "acceptance_criteria": ["The same task reuses its feature branch"],
    }
    first_job = SimpleNamespace(
        payload=payload,
        task_id="a1b2c3d4-e5f6-4789-9012-abcdefabcdef",
        tenant_id="tenant-a",
        lease_token="lease-token-first",
    )
    retry_job = SimpleNamespace(
        payload=payload,
        task_id="a1b2c3d4-e5f6-4789-9012-abcdefabcdef",
        tenant_id="tenant-a",
        lease_token="lease-token-second",
    )

    first = AICodeRuntime._task(first_job)
    retry = AICodeRuntime._task(retry_job)

    assert first.repository.work_branch == retry.repository.work_branch
    assert first.repository.work_branch == "feature/aicode/a1b2c3d4e5f647899012abcdefabcdef"
    assert first.metadata["lease_token"] != retry.metadata["lease_token"]
