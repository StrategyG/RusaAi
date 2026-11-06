"""Bounded hierarchical DAG orchestrator with retry and evidence gates."""
import asyncio
from dataclasses import dataclass

from .audit import AuditLog
from .contracts import TaskRecord, TaskSpec, TaskState, WorkerResult
from .evaluation import ResultEvaluator
from .memory import SQLiteMemory
from .policy import AutonomyPolicy
from .workers import WorkerRegistry


@dataclass(frozen=True)
class OrchestrationReport:
    task_records: dict[str, TaskRecord]
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    blocked: tuple[str, ...]


class DAGOrchestrator:
    def __init__(
        self,
        workers: WorkerRegistry,
        *,
        policy: AutonomyPolicy | None = None,
        memory: SQLiteMemory | None = None,
        audit: AuditLog | None = None,
        evaluator: ResultEvaluator | None = None,
        max_concurrency: int = 8,
        max_attempts: int = 2,
    ) -> None:
        if max_concurrency < 1 or max_attempts < 1:
            raise ValueError("max_concurrency and max_attempts must be positive")
        self.workers = workers
        self.policy = policy or AutonomyPolicy()
        self.memory = memory
        self.audit = audit
        self.evaluator = evaluator or ResultEvaluator()
        self.max_concurrency = max_concurrency
        self.max_attempts = max_attempts

    @staticmethod
    def _validate_plan(tasks: tuple[TaskSpec, ...]) -> None:
        by_id = {task.task_id: task for task in tasks}
        if len(by_id) != len(tasks):
            raise ValueError("task IDs must be unique")
        for task in tasks:
            unknown = set(task.dependencies) - set(by_id)
            if unknown:
                raise ValueError(f"unknown dependencies for {task.task_id}: {sorted(unknown)}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependency graph contains a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in by_id:
            visit(task_id)

    def _persist(self, record: TaskRecord) -> None:
        if self.memory is None:
            return
        self.memory.save_task(
            record.spec.tenant_id,
            record.spec.project_id,
            record.spec.task_id,
            record.state.value,
            {
                "objective": record.spec.objective,
                "capability": record.spec.capability,
                "attempts": record.attempts,
                "error": record.error,
                "result": record.result.summary if record.result else None,
            },
        )

    def _audit(self, record: TaskRecord, event: str, payload: dict) -> None:
        if self.audit is not None:
            self.audit.append(
                event_id=f"{record.spec.task_id}:{event}:{record.attempts}",
                event_type=event,
                tenant_id=record.spec.tenant_id,
                actor_id=record.spec.requested_by,
                payload=payload,
            )

    async def _execute_one(
        self,
        record: TaskRecord,
        context: list[WorkerResult],
        semaphore: asyncio.Semaphore,
    ) -> None:
        decision = self.policy.evaluate("agent.execute")
        if not decision.allowed:
            record.error = decision.reason
            record.transition(TaskState.BLOCKED)
            self._persist(record)
            self._audit(record, "task.blocked", {"reason": decision.reason})
            return

        worker = self.workers.get(record.spec.capability)
        async with semaphore:
            record.transition(TaskState.RUNNING)
            self._persist(record)
            self._audit(record, "task.started", {"capability": record.spec.capability})
            last_error = None
            for attempt in range(1, self.max_attempts + 1):
                record.attempts = attempt
                try:
                    result = await worker.run(record.spec, list(context))
                    if result.task_id != record.spec.task_id:
                        raise ValueError("worker result task_id does not match assigned task")
                    report = self.evaluator.evaluate(record.spec, result)
                    record.result = result
                    if report.passed:
                        record.transition(TaskState.VERIFYING)
                        record.transition(TaskState.SUCCEEDED)
                        self._audit(
                            record,
                            "task.verified",
                            {"score": report.score, "evidence_count": len(result.evidence)},
                        )
                        self._persist(record)
                        return
                    last_error = "; ".join(report.reasons) or "evaluation failed"
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                self._audit(record, "task.retry", {"attempt": attempt, "error": last_error})
            record.error = last_error or "worker failed"
            record.transition(TaskState.FAILED)
            self._persist(record)
            self._audit(record, "task.failed", {"error": record.error})

    async def run(self, tasks: tuple[TaskSpec, ...]) -> OrchestrationReport:
        self._validate_plan(tasks)
        records = {task.task_id: TaskRecord(spec=task) for task in tasks}
        for record in records.values():
            record.transition(TaskState.QUEUED)
            self._persist(record)

        semaphore = asyncio.Semaphore(self.max_concurrency)
        pending = set(records)
        completed_results: dict[str, WorkerResult] = {}

        while pending:
            ready = [
                task_id
                for task_id in pending
                if all(
                    records[dependency].state == TaskState.SUCCEEDED
                    for dependency in records[task_id].spec.dependencies
                )
            ]
            blocked = [
                task_id
                for task_id in pending
                if any(
                    records[dependency].state in {
                        TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED
                    }
                    for dependency in records[task_id].spec.dependencies
                )
            ]
            for task_id in blocked:
                record = records[task_id]
                record.transition(TaskState.BLOCKED)
                record.error = "one or more dependencies did not succeed"
                self._persist(record)
                pending.remove(task_id)

            if not ready:
                if pending:
                    raise RuntimeError("orchestration stalled; unresolved task dependencies")
                break

            await asyncio.gather(
                *(
                    self._execute_one(
                        records[task_id],
                        [
                            completed_results[dependency]
                            for dependency in records[task_id].spec.dependencies
                        ],
                        semaphore,
                    )
                    for task_id in ready
                )
            )
            for task_id in ready:
                result = records[task_id].result
                if records[task_id].state == TaskState.SUCCEEDED and result is not None:
                    completed_results[task_id] = result
                pending.remove(task_id)

        succeeded = tuple(
            task_id for task_id, record in records.items()
            if record.state == TaskState.SUCCEEDED
        )
        failed = tuple(
            task_id for task_id, record in records.items()
            if record.state == TaskState.FAILED
        )
        blocked_ids = tuple(
            task_id for task_id, record in records.items()
            if record.state == TaskState.BLOCKED
        )
        return OrchestrationReport(records, succeeded, failed, blocked_ids)
