import asyncio

from rusa_agents.contracts import CodingTask, RepositoryTarget, TaskRecord, TaskState
from rusa_agents.worker import QueueWorker


def make_task():
    return CodingTask(
        tenant_id="tenant-heartbeat",
        objective="Verify worker lease heartbeat",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch="test/heartbeat",
        ),
        acceptance_criteria=("Lease remains owned",),
    )


class FakeQueue:
    def __init__(self, task):
        self.task = task
        self.renewals = 0
        self.acknowledged = None
        self.retried = None
        self.checkpointed = None

    def claim(self, lease_seconds):
        return {
            "tenant_id": self.task.tenant_id,
            "task_id": self.task.task_id,
            "payload": self.task.to_dict(),
            "result": None,
            "lease_token": "x" * 43,
        }

    def renew_lease(self, tenant_id, task_id, lease_token, lease_seconds):
        assert tenant_id == self.task.tenant_id
        assert task_id == self.task.task_id
        assert lease_token == "x" * 43
        self.renewals += 1
        return True

    def checkpoint(self, tenant_id, task_id, lease_token, record):
        assert lease_token == "x" * 43
        self.checkpointed = (tenant_id, task_id, lease_token, record.state)
        return True

    def acknowledge(self, tenant_id, task_id, record, lease_token):
        self.acknowledged = (tenant_id, task_id, record.state, lease_token)

    def retry(self, tenant_id, task_id, error, lease_token, *, delay_seconds=10):
        self.retried = (tenant_id, task_id, error, lease_token)


class FakeOrchestrator:
    async def run(self, task, checkpoint=None, *, checkpoint_callback=None):
        await asyncio.sleep(0.06)
        record = TaskRecord(task=task)
        record.transition(TaskState.OBSERVING)
        record.transition(TaskState.PLANNING)
        record.transition(TaskState.EXECUTING)
        record.transition(TaskState.VERIFYING)
        record.transition(TaskState.BLOCKED)
        if checkpoint_callback is not None:
            await checkpoint_callback(record)
        return record


class FakeMemory:
    def store(self, *args, **kwargs):
        return 1


def test_worker_renews_lease_during_long_task():
    task = make_task()
    queue = FakeQueue(task)
    worker = QueueWorker(
        queue=queue,
        orchestrator=FakeOrchestrator(),
        memory=FakeMemory(),
        lease_seconds=10,
        heartbeat_interval_seconds=0.01,
    )
    assert asyncio.run(worker.run_once()) is True
    assert queue.renewals >= 1
    assert queue.acknowledged == (
        task.tenant_id, task.task_id, TaskState.BLOCKED, "x" * 43,
    )
    assert queue.retried is None
    assert queue.checkpointed == (
        task.tenant_id, task.task_id, "x" * 43, TaskState.BLOCKED,
    )



def test_worker_fails_closed_on_invalid_claim_token():
    task = make_task()
    queue = FakeQueue(task)

    def claim_invalid(lease_seconds):
        item = {
            "tenant_id": task.tenant_id,
            "task_id": task.task_id,
            "payload": task.to_dict(),
            "result": None,
            "lease_token": "invalid",
        }
        return item

    queue.claim = claim_invalid
    worker = QueueWorker(
        queue=queue,
        orchestrator=FakeOrchestrator(),
        memory=FakeMemory(),
        lease_seconds=10,
        heartbeat_interval_seconds=0.01,
    )
    assert asyncio.run(worker.run_once()) is True
    assert queue.acknowledged is None
    assert queue.retried is None
    assert queue.checkpointed is None


def test_worker_claim_error_is_retried_by_poll_loop():
    class BrokenQueue:
        def claim(self, lease_seconds):
            raise RuntimeError("temporary database outage")

    worker = QueueWorker(
        queue=BrokenQueue(),
        orchestrator=FakeOrchestrator(),
        memory=FakeMemory(),
    )
    assert asyncio.run(worker.run_once()) is False
