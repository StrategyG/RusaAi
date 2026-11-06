import asyncio

from rusa_agents.contracts import CodingTask, RepositoryTarget, TaskRecord, TaskState
from rusa_agents.orchestrator import CheckpointPersistenceError
from rusa_agents.worker import LeaseLostError, QueueWorker


def make_task():
    tenant_id = "tenant-worker-test"
    task_id = "worker-lease-task"
    idempotency_key = "worker-lease-idempotency"
    work_branch = CodingTask.expected_work_branch_for(
        tenant_id, idempotency_key, task_id
    )
    return CodingTask(
        tenant_id=tenant_id,
        objective="Verify worker lease fencing",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=work_branch,
        ),
        acceptance_criteria=("Lease token is persisted",),
        idempotency_key=idempotency_key,
        task_id=task_id,
    )


class FakeQueue:
    def __init__(self, task, *, include_token=True, checkpoint_result=True):
        self.task = task
        self.include_token = include_token
        self.checkpoint_result = checkpoint_result
        self.token = "lease-token-" + "x" * 40
        self.checkpoints = []
        self.heartbeats = []
        self.acknowledgements = []
        self.retries = []

    def claim(self, worker_id, lease_seconds):
        result = {
            "tenant_id": self.task.tenant_id,
            "task_id": self.task.task_id,
            "payload": self.task.to_dict(),
            "result": None,
            "lease_owner": worker_id,
        }
        if self.include_token:
            result["lease_token"] = self.token
        return result

    def checkpoint(self, tenant_id, task_id, worker_id, lease_token, record):
        self.checkpoints.append((tenant_id, task_id, worker_id, lease_token))
        return self.checkpoint_result

    def heartbeat(self, tenant_id, task_id, worker_id, lease_token, lease_seconds):
        self.heartbeats.append((tenant_id, task_id, worker_id, lease_token))
        return True

    def owns_lease(self, tenant_id, task_id, worker_id, lease_token):
        return lease_token == self.token

    def acknowledge(self, tenant_id, task_id, record, worker_id, lease_token):
        self.acknowledgements.append((tenant_id, task_id, worker_id, lease_token))

    def retry(self, tenant_id, task_id, error, worker_id, lease_token):
        self.retries.append((tenant_id, task_id, worker_id, lease_token))
        return True


class FakeOrchestrator:
    async def run(self, task, checkpoint=None, *, checkpoint_callback=None, tool_guard=None):
        assert checkpoint_callback is not None
        assert tool_guard is not None
        assert await tool_guard() is True
        record = checkpoint or TaskRecord(task=task)
        if record.state == TaskState.RECEIVED:
            record.transition(TaskState.OBSERVING)
        try:
            await checkpoint_callback(record)
        except LeaseLostError as exc:
            raise CheckpointPersistenceError(
                "durable task checkpoint could not be persisted"
            ) from exc
        return record


class FakeMemory:
    def store(self, *args, **kwargs):
        raise AssertionError("non-terminal task must not be stored as a success")

    def purge_expired(self):
        return 0


def build_worker(queue):
    return QueueWorker(
        queue=queue,
        orchestrator=FakeOrchestrator(),
        memory=FakeMemory(),
        worker_id="worker-test",
        lease_seconds=30,
    )


def test_worker_propagates_lease_token_to_checkpoint_and_acknowledgement():
    task = make_task()
    queue = FakeQueue(task)
    worker = build_worker(queue)

    assert asyncio.run(worker.run_once()) is True
    assert queue.checkpoints == [
        (task.tenant_id, task.task_id, "worker-test", queue.token)
    ]
    assert queue.acknowledgements == [
        (task.tenant_id, task.task_id, "worker-test", queue.token)
    ]
    assert queue.retries == []


def test_worker_does_not_acknowledge_after_checkpoint_lease_loss():
    task = make_task()
    queue = FakeQueue(task, checkpoint_result=False)
    worker = build_worker(queue)

    assert asyncio.run(worker.run_once()) is True
    assert queue.acknowledgements == []
    assert queue.retries == []


def test_worker_fails_closed_when_claim_has_no_lease_token():
    task = make_task()
    queue = FakeQueue(task, include_token=False)
    worker = build_worker(queue)

    assert asyncio.run(worker.run_once()) is True
    assert queue.checkpoints == []
    assert queue.acknowledgements == []
    assert queue.retries == []


def test_worker_heartbeat_uses_current_lease_token(monkeypatch):
    task = make_task()
    queue = FakeQueue(task)
    worker = build_worker(queue)
    original_wait = asyncio.wait
    calls = 0

    async def immediate_first_wait(tasks, timeout=None, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return set(), set(tasks)
        return await original_wait(tasks, timeout=timeout, **kwargs)

    monkeypatch.setattr("rusa_agents.worker.asyncio.wait", immediate_first_wait)
    assert asyncio.run(worker.run_once()) is True
    assert queue.heartbeats == [
        (task.tenant_id, task.task_id, "worker-test", queue.token)
    ]
