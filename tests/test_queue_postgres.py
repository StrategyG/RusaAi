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


def make_payload(tenant_id, objective="exercise queue"):
    return {
        "task_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
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
    payload = make_payload(tenant)
    payload["task_id"] = task_id
    first = queue.enqueue(tenant, task_id, payload, "same-idempotency-key")
    replay = queue.enqueue(tenant, task_id, payload, "same-idempotency-key")
    assert replay["task_id"] == first["task_id"]

    changed = make_payload(tenant, "different objective")
    with pytest.raises(ValueError, match="different task payload"):
        queue.enqueue(tenant, changed["task_id"], changed, "same-idempotency-key")


def test_lease_owner_can_heartbeat_and_complete(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = make_payload(tenant)
    payload["task_id"] = task_id
    queue.enqueue(tenant, task_id, payload, "lease-ownership-key")
    job = queue.claim("worker-a", lease_seconds=60)
    assert job is not None
    assert job["task_id"] == task_id
    assert queue.claim("worker-b", lease_seconds=60) is None
    assert not queue.heartbeat(tenant, task_id, "worker-b", job["lease_token"], 60)
    assert queue.heartbeat(tenant, task_id, "worker-a", job["lease_token"], 60)
    assert not queue.complete(tenant, task_id, "worker-b", job["lease_token"], {"ok": True})
    assert queue.complete(tenant, task_id, "worker-a", job["lease_token"], {"ok": True})
    assert queue.get(tenant, task_id)["state"] == "succeeded"


def test_retry_limit_transitions_job_to_failed(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = make_payload(tenant)
    payload["task_id"] = task_id
    queue.enqueue(tenant, task_id, payload, "retry-limit-key", max_attempts=2)
    first = queue.claim("worker-a", lease_seconds=60)
    assert first["attempts"] == 1
    assert queue.fail(
        tenant, task_id, "worker-a", first["lease_token"], "temporary", retryable=True
    )

    with queue._connect() as db:
        db.execute(
            "UPDATE rusa_agent_jobs SET available_at=now() WHERE tenant_id=%s AND task_id=%s::uuid",
            (tenant, task_id),
        )
    second = queue.claim("worker-a", lease_seconds=60)
    assert second["attempts"] == 2
    assert queue.fail(
        tenant, task_id, "worker-a", second["lease_token"], "final", retryable=True
    )
    assert queue.get(tenant, task_id)["state"] == "dead_letter"


def test_expired_final_attempt_is_terminalized(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = make_payload(tenant)
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
    assert queue.get(tenant, task_id)["state"] == "dead_letter"


def test_queue_rejects_invalid_lease_duration(queue_and_tenant):
    queue, _ = queue_and_tenant
    with pytest.raises(ValueError, match="lease_seconds"):
        queue.claim("worker-a", lease_seconds=1)



def test_queue_rejects_payload_task_id_mismatch(queue_and_tenant):
    queue, tenant = queue_and_tenant
    with pytest.raises(ValueError, match="must match"):
        queue.enqueue(
            tenant,
            str(uuid.uuid4()),
            {"task_id": str(uuid.uuid4()), "objective": "mismatch"},
            "payload-task-id-key",
        )


def test_queue_rejects_unsafe_tenant_identifiers(queue_and_tenant):
    queue, _ = queue_and_tenant
    with pytest.raises(ValueError, match="safe characters"):
        queue.enqueue(
            "tenant/other",
            str(uuid.uuid4()),
            {"objective": "unsafe tenant"},
            "unsafe-tenant-key",
        )



def test_queue_cancels_queued_task_idempotently(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    queue.enqueue(
        tenant,
        task_id,
        {"task_id": task_id, "tenant_id": tenant, "objective": "cancel me"},
        "cancel-queued-key",
    )
    assert queue.cancel(tenant, task_id)
    assert queue.cancel(tenant, task_id)
    assert queue.get(tenant, task_id)["state"] == "cancelled"
    assert queue.claim("worker-a", lease_seconds=60) is None


def test_queue_cancellation_fences_running_worker(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    queue.enqueue(
        tenant,
        task_id,
        {"task_id": task_id, "tenant_id": tenant, "objective": "cancel active"},
        "cancel-running-key",
    )
    job = queue.claim("worker-a", lease_seconds=60)
    assert job is not None
    assert queue.cancel(tenant, task_id)
    assert not queue.heartbeat(tenant, task_id, "worker-a", job["lease_token"], lease_seconds=60)
    assert not queue.complete(
        tenant, task_id, "worker-a", job["lease_token"], {"state": "succeeded"}
    )
    assert queue.get(tenant, task_id)["state"] == "cancelled"


def test_queue_does_not_cancel_completed_task(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    queue.enqueue(
        tenant,
        task_id,
        {"task_id": task_id, "tenant_id": tenant, "objective": "complete"},
        "cancel-completed-key",
    )
    job = queue.claim("worker-a", lease_seconds=60)
    assert job is not None
    assert queue.complete(tenant, task_id, "worker-a", job["lease_token"], {"state": "succeeded"})
    assert not queue.cancel(tenant, task_id)
    assert queue.get(tenant, task_id)["state"] == "succeeded"



def test_queue_rejects_payload_tenant_mismatch(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="tenant_id must match"):
        queue.enqueue(
            tenant,
            task_id,
            {
                "task_id": task_id,
                "tenant_id": "another-tenant",
                "objective": "cross-tenant payload",
            },
            "payload-tenant-mismatch",
        )


def test_queue_requires_payload_task_id(queue_and_tenant):
    queue, tenant = queue_and_tenant
    with pytest.raises(ValueError, match="include task_id"):
        queue.enqueue(
            tenant,
            str(uuid.uuid4()),
            {"tenant_id": tenant, "objective": "missing id"},
            "missing-payload-task-id",
        )



def test_dead_letter_listing_and_manual_replay_are_tenant_scoped(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    payload = {
        "task_id": task_id,
        "tenant_id": tenant,
        "objective": "replay after operator review",
    }
    queue.enqueue(tenant, task_id, payload, "dead-letter-replay-key", max_attempts=1)
    job = queue.claim("worker-a", lease_seconds=60)
    assert job is not None
    assert queue.fail(
        tenant, task_id, "worker-a", job["lease_token"],
        "permanent test failure", retryable=True
    )

    dead_letters = queue.list_dead_letters(tenant, limit=10)
    assert [item["task_id"] for item in dead_letters] == [task_id]
    assert queue.list_dead_letters("different-tenant", limit=10) == []
    assert queue.retry_dead_letter("different-tenant", task_id) is False
    assert queue.retry_dead_letter(tenant, task_id) is True
    assert queue.get(tenant, task_id)["state"] == "queued"
    assert queue.get(tenant, task_id)["attempts"] == 0
    assert queue.retry_dead_letter(tenant, task_id) is False


def test_queue_stats_are_tenant_scoped_and_include_queue_age(queue_and_tenant):
    queue, tenant = queue_and_tenant
    task_id = str(uuid.uuid4())
    queue.enqueue(
        tenant,
        task_id,
        {"task_id": task_id, "tenant_id": tenant, "objective": "stats"},
        "queue-stats-key",
    )
    stats = queue.stats(tenant)
    assert stats["tenant_id"] == tenant
    assert stats["counts"]["queued"] == 1
    assert stats["oldest_queued_age_seconds"] >= 0
    assert queue.stats("another-tenant")["counts"]["queued"] == 0
