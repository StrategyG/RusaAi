import asyncio
from datetime import datetime, timezone
from uuid import uuid4

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


def stamped_evidence(task, role, evidence):
    now = datetime.now(timezone.utc).isoformat()
    stamped = {}
    for key, value in evidence.items():
        if not isinstance(value, dict):
            continue
        profile = key.removeprefix("check:")
        item = dict(value)
        item.setdefault("profile", profile)
        item.setdefault("worker_role", role.value)
        item.setdefault("task_id", task.task_id)
        item.setdefault("attempt", 1)
        item.setdefault("workspace_digest", "a" * 64)
        item.setdefault("output_digest", "b" * 64)
        item.setdefault("execution_id", uuid4().hex)
        item.setdefault("started_at", now)
        item.setdefault("completed_at", now)
        item.setdefault("duration_seconds", 0.0)
        item.setdefault("image", "sandbox:test")
        item.setdefault("snapshot_bytes", 1)
        stamped[key] = item
    return stamped


class StubWorker:
    def __init__(self, role, evidence=None):
        self.role = role
        self.evidence = evidence or {}

    async def run(self, task, prior_results):
        return WorkerResult(
            role=self.role, summary="ok", success=True,
            artifacts=["src/main.py"] if self.role == WorkerRole.DEVELOPER else [],
            evidence=stamped_evidence(task, self.role, self.evidence),
        )


def task():
    return CodingTask(
        objective="Implement AICode",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/test"),
        acceptance_criteria=["Tests pass", "Security review passes"],
    )


def workers(with_evidence=True):
    evidence = {"check:pytest": {"exit_code": 0}} if with_evidence else {}
    security = {
        "check:bandit": {"exit_code": 0},
        "check:ruff": {"exit_code": 0},
        "check:compile": {"exit_code": 0},
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
    failing[-2].evidence = {"check:pytest": {"exit_code": 1}}
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
                evidence = {"check:pytest": {"exit_code": 0}}
            elif role == WorkerRole.SECURITY:
                evidence = {
                    "check:bandit": {"exit_code": 0},
                    "check:ruff": {"exit_code": 0},
                    "check:compile": {"exit_code": 0},
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
                ParallelWorker(role, {"check:pytest": {"exit_code": 0}})
            )
        elif role == WorkerRole.SECURITY:
            evidence_workers.append(
                ParallelWorker(
                    role,
                    {
                        "check:bandit": {"exit_code": 0},
                        "check:ruff": {"exit_code": 0},
                        "check:compile": {"exit_code": 0},
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
                artifacts=["src/main.py"],
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
                evidence=stamped_evidence(
                    task, self.role,
                    {"check:pytest": {"exit_code": 1 if self.calls == 1 else 0}},
                ),
            )

    class CountingSecurity(StubWorker):
        def __init__(self):
            super().__init__(
                WorkerRole.SECURITY,
                {
                    "check:bandit": {"exit_code": 0},
                    "check:ruff": {"exit_code": 0},
                    "check:compile": {"exit_code": 0},
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


def test_orchestrator_rejects_boolean_exit_code_as_evidence(tmp_path):
    configured = workers()
    configured[-2].evidence = {"check:pytest": {"exit_code": False}}
    orchestrator = AICodeOrchestrator(
        configured,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        max_repair_cycles=0,
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL
    assert any("invalid provenance" in error for error in record.errors)


def test_orchestrator_rejects_mixed_workspace_evidence(tmp_path):
    configured = workers()
    configured[-2].evidence = {
        "check:pytest": {"exit_code": 0, "workspace_digest": "a" * 64}
    }
    configured[-1].evidence = {
        "check:bandit": {"exit_code": 0, "workspace_digest": "b" * 64},
        "check:ruff": {"exit_code": 0, "workspace_digest": "b" * 64},
        "check:compile": {"exit_code": 0, "workspace_digest": "b" * 64},
    }
    orchestrator = AICodeOrchestrator(
        configured,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        max_repair_cycles=0,
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL
    assert any("workspace snapshot" in error for error in record.errors)


def test_orchestrator_repair_receives_quality_diagnostics(tmp_path):
    class DiagnosticDeveloper(StubWorker):
        def __init__(self):
            super().__init__(WorkerRole.DEVELOPER)
            self.repair_context = None
            self.calls = 0

        async def run(self, task, prior_results):
            self.calls += 1
            if self.calls > 1:
                self.repair_context = next(
                    (item for item in prior_results if item.failure_kind == "quality_gate"),
                    None,
                )
            return WorkerResult(
                role=self.role, summary="implemented", success=True, artifacts=["src/main.py"]
            )

    class OneFailTester(StubWorker):
        def __init__(self):
            super().__init__(WorkerRole.TESTER)
            self.calls = 0

        async def run(self, task, prior_results):
            self.calls += 1
            return WorkerResult(
                role=self.role, summary="pytest report",
                success=True, evidence=stamped_evidence(
                    task, self.role,
                    {"check:pytest": {"exit_code": 1 if self.calls == 1 else 0}},
                ),
            )

    developer = DiagnosticDeveloper()
    configured = [
        StubWorker(WorkerRole.ANALYST), StubWorker(WorkerRole.ARCHITECT),
        developer, StubWorker(WorkerRole.DEBUGGER), OneFailTester(),
        StubWorker(WorkerRole.SECURITY, {
            "check:bandit": {"exit_code": 0},
            "check:ruff": {"exit_code": 0},
            "check:compile": {"exit_code": 0},
        }),
    ]
    orchestrator = AICodeOrchestrator(
        configured,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        max_repair_cycles=1,
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.SUCCEEDED
    assert developer.repair_context is not None
    assert "check:pytest" in " ".join(developer.repair_context.blockers)


def test_orchestrator_requires_complete_task_bound_provenance(tmp_path):
    configured = workers()
    configured[-2].evidence = {
        "check:pytest": {
            "exit_code": 0,
            "task_id": "another-task",
        }
    }
    orchestrator = AICodeOrchestrator(
        configured,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        max_repair_cycles=0,
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL
    assert any("invalid provenance" in error for error in record.errors)


def test_orchestrator_requires_a_changed_developer_artifact(tmp_path):
    class NoopDeveloper(StubWorker):
        async def run(self, task, prior_results):
            return WorkerResult(role=self.role, summary="No code changes needed", success=True)

    configured = workers()
    configured[2] = NoopDeveloper(WorkerRole.DEVELOPER)
    orchestrator = AICodeOrchestrator(
        configured,
        memory=SQLiteMemory(str(tmp_path / "memory.sqlite3")),
        audit=AuditLog(str(tmp_path / "audit.jsonl")),
        max_repair_cycles=0,
    )
    record = asyncio.run(orchestrator.run(task()))
    assert record.state == TaskState.AWAITING_APPROVAL
    assert any("no changed workspace artifacts" in error for error in record.errors)
    assert not any(result.role == WorkerRole.TESTER for result in record.results)
