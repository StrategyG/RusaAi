"""PostgreSQL integration tests for queue idempotency and lease recovery."""

import os
import uuid

import pytest

from rusa_agents.queue import PostgresTaskQueue


@pytest.fixture
def queue_and_tenant():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    queue = PostgresTaskQueue(dsn)
    tenant = f"ci-{uuid.uuid4().hex}"
    try:
        yield queue, tenant
    finally:
        with queue._connect() as db:
            db.execute("DELETE FROM rusa_agent_jobs WHERE tenant_id=%s", (tenant,))


def make_payload(objective="exercise queue"):
    return {
        "task_id": str(uuid.uuid4()),
        "objective": objective,
        "repository": {
            "full_name": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "feature/queue-integration-test",
        },
        "acceptance_criteria": ["queue is durable"],
    }


def test_idempotency_returns_existing_task_and_rejects_changed_payload(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = make_payload()
    payload["task_id"] = task_id
    first = queue.enqueue(tenant, task_id, payload, "same-idempotency-key")
    replay = queue.enqueue(tenant, task_id, payload, "same-idempotency-key")
    assert replay["task_id"] == first["task_id"]

    changed = make_payload("different objective")
    with pytest.raises(ValueError, match="different task payload"):
        queue.enqueue(tenant, changed["task_id"], changed, "same-idempotency-key")


def test_lease_owner_can_heartbeat_and_complete(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = make_payload()
    payload["task_id"] = task_id
    queue.enqueue(tenant, task_id, payload, "lease-ownership-key")
    job = queue.claim("worker-a", lease_seconds=60)
    assert job is not None
    assert job["task_id"] == task_id
    assert queue.claim("worker-b", lease_seconds=60) is None
    assert not queue.heartbeat(tenant, task_id, "worker-b", 60)
    assert queue.heartbeat(tenant, task_id, "worker-a", 60)
    assert not queue.complete(tenant, task_id, "worker-b", {"ok": True})
    assert queue.complete(tenant, task_id, "worker-a", {"ok": True})
    assert queue.get(tenant, task_id)["state"] == "succeeded"


def test_retry_limit_transitions_job_to_failed(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = make_payload()
    payload["task_id"] = task_id
    queue.enqueue(tenant, task_id, payload, "retry-limit-key", max_attempts=2)
    first = queue.claim("worker-a", lease_seconds=60)
    assert first["attempts"] == 1
    assert queue.fail(tenant, task_id, "worker-a", "temporary", retryable=True)

    with queue._connect() as db:
        db.execute(
            "UPDATE rusa_agent_jobs SET available_at=now() WHERE tenant_id=%s AND task_id=%s::uuid",
            (tenant, task_id),
        )
    second = queue.claim("worker-a", lease_seconds=60)
    assert second["attempts"] == 2
    assert queue.fail(tenant, task_id, "worker-a", "final", retryable=True)
    assert queue.get(tenant, task_id)["state"] == "failed"


def test_expired_final_attempt_is_terminalized(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = make_payload()
    payload["task_id"] = task_id
    queue.enqueue(tenant, task_id, payload, "expired-final-lease-key", max_attempts=1)
    job = queue.claim("worker-a", lease_seconds=60)
    assert job["attempts"] == 1

    with queue._connect() as db:
        db.execute(
            """
            UPDATE rusa_agent_jobs
            SET lease_until=now() - interval '1 second'
            WHERE tenant_id=%s AND task_id=%s::uuid
            """,
            (tenant, task_id),
        )
    assert queue.claim("worker-b", lease_seconds=60) is None
    assert queue.get(tenant, task_id)["state"] == "failed"


def test_queue_rejects_invalid_lease_duration(queue_and_tenant):
    queue, _ = queue_and_tenant
    with pytest.raises(ValueError, match="lease_seconds"):
        queue.claim("worker-a", lease_seconds=1)
