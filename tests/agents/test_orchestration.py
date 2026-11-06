import asyncio

from rusa_agents.contracts import TaskSpec, WorkerResult
from rusa_agents.orchestrator import DAGOrchestrator
from rusa_agents.workers import WorkerRegistry


class StubWorker:
    worker_id = "stub-worker"
    capability = "review"

    async def run(self, task, context):
        return WorkerResult(
            task_id=task.task_id,
            worker_id=self.worker_id,
            success=True,
            summary="verified",
            completed_criteria=task.acceptance_criteria,
        )


def make_task(task_id, dependencies=()):
    return TaskSpec(
        task_id=task_id,
        objective="Review code",
        acceptance_criteria=("Review complete",),
        tenant_id="tenant-a",
        project_id="project-a",
        capability="review",
        dependencies=dependencies,
    )


def test_dag_runs_dependent_tasks_in_order():
    registry = WorkerRegistry()
    registry.register(StubWorker())
    report = asyncio.run(
        DAGOrchestrator(registry).run(
            (make_task("task-a"), make_task("task-b", ("task-a",)))
        )
    )
    assert report.succeeded == ("task-a", "task-b")
    assert report.failed == ()


def test_worker_result_cannot_claim_another_task():
    class WrongTaskWorker(StubWorker):
        async def run(self, task, context):
            return WorkerResult(
                task_id="wrong-task",
                worker_id=self.worker_id,
                success=True,
                summary="forged",
                completed_criteria=task.acceptance_criteria,
            )

    registry = WorkerRegistry()
    registry.register(WrongTaskWorker())
    report = asyncio.run(DAGOrchestrator(registry).run((make_task("task-a"),)))
    assert report.failed == ("task-a",)


def test_failed_dependency_blocks_downstream():
    class FailingWorker(StubWorker):
        async def run(self, task, context):
            return WorkerResult(
                task_id=task.task_id,
                worker_id=self.worker_id,
                success=False,
                summary="failed",
            )

    registry = WorkerRegistry()
    registry.register(FailingWorker())
    report = asyncio.run(
        DAGOrchestrator(registry).run(
            (make_task("task-a"), make_task("task-b", ("task-a",)))
        )
    )
    assert report.failed == ("task-a",)
    assert report.blocked == ("task-b",)


def test_plan_cycle_is_rejected():
    registry = WorkerRegistry()
    registry.register(StubWorker())
    try:
        asyncio.run(
            DAGOrchestrator(registry).run(
                (
                    make_task("task-a", ("task-b",)),
                    make_task("task-b", ("task-a",)),
                )
            )
        )
    except ValueError as exc:
        assert "cycle" in str(exc)
    else:
        raise AssertionError("cycle should be rejected")
