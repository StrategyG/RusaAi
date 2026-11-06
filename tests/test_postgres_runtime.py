"""PostgreSQL integration tests; CI supplies TEST_DATABASE_URL."""

from __future__ import annotations

import os
import uuid

import pytest

from rusa_agents.audit import PostgresAuditLog
from rusa_agents.memory import PostgresMemory
from rusa_agents.queue import PostgresTaskQueue


@pytest.fixture
def postgres_runtime():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not configured")
    tenant_id = f"pytest-{uuid.uuid4().hex}"
    queue = PostgresTaskQueue(dsn)
    yield dsn, tenant_id, queue
    with queue._connect() as db:
        db.execute("DELETE FROM rusa_agent_jobs WHERE tenant_id=%s", (tenant_id,))
        db.execute("DELETE FROM rusa_agent_memory WHERE tenant_id=%s", (tenant_id,))


def test_queue_idempotency_replays_only_identical_payload(postgres_runtime):
    _, tenant_id, queue = postgres_runtime
    first_id = str(uuid.uuid4())
    replay_id = str(uuid.uuid4())
    payload = {"objective": "review", "repository": "owner/repo"}

    first = queue.enqueue(tenant_id, first_id, payload, "same-idempotency-key")
    replay = queue.enqueue(tenant_id, replay_id, payload, "same-idempotency-key")

    assert first["task_id"] == first_id
    assert replay["task_id"] == first_id
    with pytest.raises(ValueError, match="different task payload"):
        queue.enqueue(
            tenant_id,
            str(uuid.uuid4()),
            {"objective": "delete", "repository": "owner/repo"},
            "same-idempotency-key",
        )


def test_queue_heartbeat_and_lease_owner_fencing(postgres_runtime):
    _, tenant_id, queue = postgres_runtime
    task_id = str(uuid.uuid4())
    queue.enqueue(tenant_id, task_id, {"objective": "test"}, "lease-key-001")
    first = queue.claim("worker-a", lease_seconds=30)
    assert first is not None
    assert queue.heartbeat(tenant_id, task_id, "worker-a", lease_seconds=30)

    with queue._connect() as db:
        db.execute(
            """
            UPDATE rusa_agent_jobs
            SET lease_until=now() - interval '1 second'
            WHERE tenant_id=%s AND task_id=%s::uuid
            """,
            (tenant_id, task_id),
        )

    second = queue.claim("worker-b", lease_seconds=30)
    assert second is not None
    assert second["task_id"] == task_id
    assert second["attempts"] == 2
    assert not queue.heartbeat(tenant_id, task_id, "worker-a", lease_seconds=30)
    assert not queue.complete(tenant_id, task_id, "worker-a", {"state": "succeeded"})
    assert queue.complete(tenant_id, task_id, "worker-b", {"state": "succeeded"})


def test_expired_job_at_retry_limit_moves_to_dead_letter(postgres_runtime):
    _, tenant_id, queue = postgres_runtime
    task_id = str(uuid.uuid4())
    queue.enqueue(tenant_id, task_id, {"objective": "retry"}, "dead-letter-key")
    claimed = queue.claim("worker-a", lease_seconds=30)
    assert claimed is not None

    with queue._connect() as db:
        db.execute(
            """
            UPDATE rusa_agent_jobs
            SET max_attempts=1, lease_until=now() - interval '1 second'
            WHERE tenant_id=%s AND task_id=%s::uuid
            """,
            (tenant_id, task_id),
        )

    assert queue.claim("worker-b", lease_seconds=30) is None
    stored = queue.get(tenant_id, task_id)
    assert stored["state"] == "dead_letter"


def test_non_retryable_failure_is_terminal(postgres_runtime):
    _, tenant_id, queue = postgres_runtime
    task_id = str(uuid.uuid4())
    queue.enqueue(tenant_id, task_id, {"objective": "blocked"}, "blocked-key")
    claimed = queue.claim("worker-a", lease_seconds=30)
    assert claimed is not None
    assert queue.fail(
        tenant_id,
        task_id,
        "worker-a",
        "human approval required",
        retryable=False,
    )
    assert queue.get(tenant_id, task_id)["state"] == "failed"


def test_postgres_memory_is_tenant_scoped(postgres_runtime):
    dsn, tenant_id, _ = postgres_runtime
    memory = PostgresMemory(dsn)
    task_id = str(uuid.uuid4())
    memory.save(task_id, "owner/repo", "executing", {"step": 2}, tenant_id=tenant_id)

    assert memory.get(task_id, tenant_id=tenant_id)["payload"]["step"] == 2
    assert memory.get(task_id, tenant_id="another-tenant") is None


def test_postgres_audit_chain_is_verifiable_and_append_only(postgres_runtime):
    dsn, tenant_id, _ = postgres_runtime
    audit = PostgresAuditLog(dsn)
    task_id = str(uuid.uuid4())
    audit.record("task.started", task_id, "pytest", {"safe": True}, tenant_id=tenant_id)
    audit.record("task.finished", task_id, "pytest", {"state": "blocked"}, tenant_id=tenant_id)
    assert audit.verify()

    with pytest.raises(Exception):
        with audit._connect() as db:
            db.execute(
                """
                UPDATE rusa_agent_audit_events
                SET payload='{}'::jsonb
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task_id),
            )
    assert audit.verify()
