import os
from uuid import uuid4

import pytest

from rusa_agents.audit import PostgresAuditLog
from rusa_agents.memory import PostgresMemory
from rusa_agents.queue import PostgresTaskQueue
from rusa_agents.rate_limit import PostgresRateLimiter

DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL integration DSN is not configured")


def test_postgres_queue_tenant_scope_and_idempotency():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    task_id = str(uuid4())
    assert queue.enqueue(task_id, "tenant-alpha", {"objective": "test"}, priority=100)
    assert not queue.enqueue(task_id, "tenant-alpha", {"objective": "duplicate"}, priority=100)
    assert queue.get(task_id, "tenant-beta") is None

    job = queue.claim("pytest-worker", lease_seconds=30)
    assert job is not None
    assert job.task_id == task_id
    assert job.tenant_id == "tenant-alpha"
    assert queue.heartbeat(job.id, job.tenant_id, "pytest-worker", job.lease_token, 30)
    assert queue.complete(job.id, job.tenant_id, "pytest-worker", job.lease_token)
    assert queue.get(task_id, "tenant-alpha")["status"] == "succeeded"


def test_postgres_queue_retries_then_dead_letters():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    task_id = str(uuid4())
    assert queue.enqueue(task_id, "tenant-retry", {"objective": "retry"}, max_attempts=2, priority=200)

    first = queue.claim("retry-worker", lease_seconds=30)
    assert first is not None and first.task_id == task_id
    assert queue.fail(first.id, first.tenant_id, "retry-worker", first.lease_token, "first failure", retry_delay_seconds=0)

    second = queue.claim("retry-worker", lease_seconds=30)
    assert second is not None and second.task_id == task_id
    assert second.attempts == 2
    assert queue.fail(second.id, second.tenant_id, "retry-worker", second.lease_token, "second failure", retry_delay_seconds=0)
    assert queue.get(task_id, "tenant-retry")["status"] == "failed"


def test_postgres_memory_and_audit_integrity():
    memory = PostgresMemory(DSN)
    memory.initialize()
    task_id = str(uuid4())
    memory.save(task_id, "StrategyG/RusaAi", "verifying", {"ok": True}, tenant_id="tenant-memory")
    assert memory.get(task_id, "tenant-memory")["payload"]["ok"] is True
    assert memory.get(task_id, "other-tenant") is None

    key = os.getenv("RUSA_AUDIT_HMAC_KEY", "ci-test-audit-key")
    audit = PostgresAuditLog(DSN, key)
    audit.initialize()
    audit.record("test.event", task_id, "pytest", {"result": "pass"}, tenant_id="tenant-memory")
    assert audit.verify()
    assert not PostgresAuditLog(DSN, "wrong-key-" + "x" * 32).verify()



def test_postgres_rate_limiter_is_tenant_scoped():
    limiter = PostgresRateLimiter(DSN)
    limiter.initialize()
    route = "pytest:" + str(uuid4())
    assert limiter.hit("tenant-rate-a", route, 1)[0] is True
    assert limiter.hit("tenant-rate-a", route, 1)[0] is False
    assert limiter.hit("tenant-rate-b", route, 1)[0] is True



def test_postgres_queue_requires_explicit_retry_after_approval():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    task_id = str(uuid4())
    assert queue.enqueue(task_id, "tenant-approval", {"objective": "approval test"})
    job = queue.claim("approval-worker", lease_seconds=30)
    assert job is not None and job.task_id == task_id
    assert queue.await_approval(job.id, job.tenant_id, "approval-worker", job.lease_token, "review required")
    assert queue.get(task_id, "tenant-approval")["status"] == "awaiting_approval"
    assert queue.retry(task_id, "wrong-tenant") is False
    assert queue.retry(task_id, "tenant-approval") is True
    assert queue.get(task_id, "tenant-approval")["status"] == "queued"



def test_postgres_queue_fences_stale_worker_after_lease_reassignment():
    import psycopg

    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    task_id = str(uuid4())
    assert queue.enqueue(task_id, "tenant-fence", {"objective": "fencing"}, priority=1000)

    stale = queue.claim("same-worker-id", lease_seconds=30)
    assert stale is not None and stale.task_id == task_id

    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE aicode_jobs SET lease_until=NOW() - INTERVAL '1 second' WHERE id=%s",
            (stale.id,),
        )

    assert queue.heartbeat(stale.id, stale.tenant_id, "same-worker-id", stale.lease_token, 30) is False
    replacement = queue.claim("same-worker-id", lease_seconds=30)
    assert replacement is not None and replacement.task_id == task_id
    assert replacement.lease_token != stale.lease_token

    assert queue.complete(stale.id, stale.tenant_id, "same-worker-id", stale.lease_token) is False
    assert queue.fail(
        stale.id, stale.tenant_id, "same-worker-id", stale.lease_token, "stale failure"
    ) is False
    assert queue.complete(
        replacement.id, replacement.tenant_id, "same-worker-id", replacement.lease_token
    ) is True
