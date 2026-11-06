import os
from concurrent.futures import ThreadPoolExecutor
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
    assert job.lease_token
    assert queue.heartbeat(job.id, job.tenant_id, "pytest-worker", job.lease_token, 30)
    result = {
        "pull_request_url": "https://github.com/StrategyG/RusaAi/pull/99",
        "commit_sha": "a" * 40,
        "branch": "feature/aicode/test-deadbeef",
    }
    assert queue.complete(
        job.id, job.tenant_id, "pytest-worker", job.lease_token, result
    )
    stored = queue.get(task_id, "tenant-alpha")
    assert stored["status"] == "succeeded"
    assert stored["result"] == result


def test_postgres_queue_idempotency_is_tenant_scoped_payload_bound_and_race_safe():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant_id = "tenant-idempotency-" + uuid4().hex[:8]
    key = "client-request-" + uuid4().hex
    request_hash = "a" * 64
    payload = {"objective": "create exactly one task"}
    created_tasks = []

    try:
        first = queue.enqueue_idempotent(
            str(uuid4()), tenant_id, payload, key, request_hash
        )
        assert first.created is True
        assert first.conflict is False
        if first.created:
            created_tasks.append((first.task_id, tenant_id))

        replay = queue.enqueue_idempotent(
            str(uuid4()), tenant_id, payload, key, request_hash
        )
        assert replay.created is False
        assert replay.conflict is False
        assert replay.task_id == first.task_id

        changed = queue.enqueue_idempotent(
            str(uuid4()), tenant_id, {"objective": "different payload"}, key, "b" * 64
        )
        assert changed.created is False
        assert changed.conflict is True
        assert changed.task_id == first.task_id

        other_tenant_id = tenant_id + "-other"
        other_tenant = queue.enqueue_idempotent(
            str(uuid4()), other_tenant_id, payload, key, request_hash
        )
        assert other_tenant.created is True
        assert other_tenant.task_id != first.task_id
        if other_tenant.created:
            created_tasks.append((other_tenant.task_id, other_tenant_id))

        race_tenant = tenant_id + "-race"
        race_key = "concurrent-" + uuid4().hex

        def submit(_):
            return queue.enqueue_idempotent(
                str(uuid4()), race_tenant, payload, race_key, request_hash
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(8)))
        assert len({item.task_id for item in results}) == 1
        assert sum(item.created for item in results) == 1
        assert all(not item.conflict for item in results)
        if any(item.created for item in results):
            created_tasks.append((results[0].task_id, race_tenant))
    finally:
        for created_task_id, created_tenant_id in created_tasks:
            queue.cancel(created_task_id, created_tenant_id)


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
    review_result = {"branch": "feature/aicode/review-deadbeef"}
    assert queue.await_approval(
        job.id, job.tenant_id, "approval-worker", job.lease_token,
        "review required", review_result,
    )
    awaiting = queue.get(task_id, "tenant-approval")
    assert awaiting["status"] == "awaiting_approval"
    assert awaiting["result"] == review_result
    assert queue.retry(task_id, "wrong-tenant") is False
    assert queue.retry(task_id, "tenant-approval") is True
    retried = queue.get(task_id, "tenant-approval")
    assert retried["status"] == "queued"
    assert retried["result"] is None



def test_postgres_queue_fences_stale_worker_after_lease_reclaim():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    task_id = str(uuid4())
    tenant_id = "tenant-fencing-" + task_id[:8]
    assert queue.enqueue(task_id, tenant_id, {"objective": "fencing"}, max_attempts=3, priority=10000)

    stale = queue.claim("stale-worker", lease_seconds=30)
    assert stale is not None and stale.task_id == task_id

    with queue._connect() as conn:
        conn.execute(
            "UPDATE aicode_jobs SET lease_until=NOW() - INTERVAL '1 second' WHERE id=%s",
            (stale.id,),
        )

    current = queue.claim("current-worker", lease_seconds=30)
    assert current is not None and current.task_id == task_id
    assert current.lease_token != stale.lease_token
    assert queue.owns_lease(current.id, tenant_id, "current-worker", current.lease_token)

    assert not queue.heartbeat(
        stale.id, tenant_id, "stale-worker", stale.lease_token, 30
    )
    assert not queue.complete(stale.id, tenant_id, "stale-worker", stale.lease_token)
    assert not queue.fail(
        stale.id, tenant_id, "stale-worker", stale.lease_token, "stale failure", 0
    )
    assert queue.complete(
        current.id, tenant_id, "current-worker", current.lease_token
    )



def test_postgres_rls_and_service_role_separation():
    import psycopg

    task_id = str(uuid4())
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL ROLE rusa_api_runtime")
        conn.execute(
            "SELECT set_config('rusa.tenant_id', %s, true)",
            ("tenant-rls-alpha",),
        )
        conn.execute(
            """
            INSERT INTO public.aicode_jobs(task_id, tenant_id, payload)
            VALUES (%s, %s, '{}'::jsonb)
            """,
            (task_id, "tenant-rls-alpha"),
        )
        visible = conn.execute(
            "SELECT tenant_id FROM public.aicode_jobs WHERE task_id=%s",
            (task_id,),
        ).fetchone()
        assert visible == ("tenant-rls-alpha",)

        conn.execute(
            "SELECT set_config('rusa.tenant_id', %s, true)",
            ("tenant-rls-beta",),
        )
        hidden = conn.execute(
            "SELECT tenant_id FROM public.aicode_jobs WHERE task_id=%s",
            (task_id,),
        ).fetchone()
        assert hidden is None

        conn.execute("SET LOCAL ROLE rusa_worker_runtime")
        worker_visible = conn.execute(
            "SELECT tenant_id FROM public.aicode_jobs WHERE task_id=%s",
            (task_id,),
        ).fetchone()
        assert worker_visible == ("tenant-rls-alpha",)

        privileges = conn.execute(
            """
            SELECT
                has_table_privilege('rusa_api_runtime', 'public.aicode_memory', 'SELECT'),
                has_table_privilege('rusa_api_runtime', 'public.aicode_audit_events', 'SELECT'),
                has_table_privilege('rusa_worker_runtime', 'public.aicode_rate_limits', 'SELECT'),
                has_table_privilege('rusa_worker_runtime', 'public.aicode_jobs', 'INSERT')
            """
        ).fetchone()
        assert privileges == (False, False, False, False)
