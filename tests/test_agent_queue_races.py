"""PostgreSQL concurrency and crash-recovery regressions for the durable queue."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.contracts import CodingTask, RepositoryTarget, TaskRecord, TaskState
from rusa_agents.queue import PostgresTaskQueue

DSN = os.getenv("RUSA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="RUSA_TEST_DATABASE_URL is not configured")


def make_task(tenant_id: str, key: str) -> CodingTask:
    task_id = uuid4().hex
    return CodingTask(
        tenant_id=tenant_id,
        objective="Exercise queue concurrency and recovery",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=CodingTask.expected_work_branch_for(tenant_id, key, task_id),
        ),
        acceptance_criteria=("Queue state remains fenced",),
        idempotency_key=key,
        task_id=task_id,
    )


def expire_lease(task_id: str, tenant_id: str) -> None:
    with psycopg.connect(DSN) as connection:
        connection.execute(
            """UPDATE rusa_agent_tasks
               SET lease_until=now() - interval '1 second'
               WHERE tenant_id=%s AND task_id=%s""",
            (tenant_id, task_id),
        )


def remove_tenant(tenant_id: str) -> None:
    with psycopg.connect(DSN) as connection:
        connection.execute("DELETE FROM rusa_agent_tasks WHERE tenant_id=%s", (tenant_id,))


def test_worker_crash_reclaims_task_and_fences_old_acknowledgement():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant = f"rusa-test-crash-{uuid4().hex}"
    task = make_task(tenant, f"crash-{uuid4().hex}")
    task_id = queue.enqueue(task, max_attempts=3)
    try:
        crashed = queue.claim("crashed-worker", lease_seconds=60)
        assert crashed and crashed["task_id"] == task_id
        old_token = crashed["lease_token"]

        # Simulate process death: no heartbeat, checkpoint, retry, or ack.
        expire_lease(task_id, tenant)
        recovered = queue.claim("recovery-worker", lease_seconds=60)
        assert recovered and recovered["task_id"] == task_id
        assert recovered["lease_token"] != old_token

        stale_record = TaskRecord(task=task)
        stale_record.transition(TaskState.OBSERVING)
        with pytest.raises(RuntimeError, match="lease was lost"):
            queue.acknowledge(
                tenant, task_id, stale_record, "crashed-worker", old_token
            )
        assert queue.owns_lease(
            tenant, task_id, "recovery-worker", recovered["lease_token"]
        )
    finally:
        remove_tenant(tenant)


def test_competing_workers_claim_distinct_tasks():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant = f"rusa-test-race-claim-{uuid4().hex}"
    first = make_task(tenant, f"first-{uuid4().hex}")
    second = make_task(tenant, f"second-{uuid4().hex}")
    first_id = queue.enqueue(first)
    second_id = queue.enqueue(second)
    try:
        def claim(worker_id):
            return queue.claim(worker_id, lease_seconds=60)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("race-worker-a", "race-worker-b")))
        claimed_ids = {item["task_id"] for item in results if item is not None}
        assert claimed_ids == {first_id, second_id}
    finally:
        remove_tenant(tenant)


def test_cancel_racing_with_success_ack_never_resurrects_terminal_task():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    queue.configure_api_permissions()
    tenant = f"rusa-test-cancel-ack-race-{uuid4().hex}"
    task = make_task(tenant, f"cancel-ack-{uuid4().hex}")
    task_id = queue.enqueue(task)
    try:
        claimed = queue.claim("race-worker", lease_seconds=60)
        assert claimed and claimed["task_id"] == task_id
        token = claimed["lease_token"]
        success = TaskRecord(task=task)
        for state in (
            TaskState.OBSERVING,
            TaskState.PLANNING,
            TaskState.EXECUTING,
            TaskState.VERIFYING,
            TaskState.SUCCEEDED,
        ):
            success.transition(state)

        def cancel():
            return queue.cancel(tenant, task_id)

        def acknowledge():
            try:
                queue.acknowledge(tenant, task_id, success, "race-worker", token)
                return "acknowledged"
            except RuntimeError:
                return "fenced"

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_future = pool.submit(cancel)
            ack_future = pool.submit(acknowledge)
            cancel_result = cancel_future.result()
            ack_result = ack_future.result()

        current = queue.get(tenant, task_id)
        assert current is not None
        assert current["status"] in {"cancelled", "succeeded"}
        if current["status"] == "cancelled":
            assert ack_result == "fenced"
            assert cancel_result is True
        else:
            assert ack_result == "acknowledged"
            assert cancel_result is False
        assert queue.owns_lease(tenant, task_id, "race-worker", token) is False
    finally:
        remove_tenant(tenant)


def test_retry_attempt_limit_is_enforced_after_expired_worker_crashes():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant = f"rusa-test-retry-limit-{uuid4().hex}"
    task = make_task(tenant, f"retry-limit-{uuid4().hex}")
    task_id = queue.enqueue(task, max_attempts=2)
    try:
        first = queue.claim("retry-worker-a", lease_seconds=60)
        assert first and first["task_id"] == task_id
        expire_lease(task_id, tenant)
        second = queue.claim("retry-worker-b", lease_seconds=60)
        assert second and second["task_id"] == task_id
        expire_lease(task_id, tenant)

        assert queue.claim("retry-worker-c", lease_seconds=60) is None
        current = queue.get(tenant, task_id)
        assert current is not None
        assert current["status"] == "failed"
        assert current["attempts"] == 2
    finally:
        remove_tenant(tenant)
