import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from rusa_agents.audit import PostgresAuditLog
from rusa_agents.memory import PostgresMemory
from rusa_agents.queue import IdempotencyConflictError, PostgresTaskQueue
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


def test_postgres_queue_idempotency_key_replays_per_tenant_and_rejects_payload_change():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant_id = f"tenant-idempotency-{uuid4().hex}"
    other_tenant = f"tenant-idempotency-other-{uuid4().hex}"
    idempotency_key = f"request-{uuid4().hex}"
    payload = {"objective": "stable task payload"}
    request_hash = hashlib.sha256(b"stable task payload").hexdigest()
    first_task_id = str(uuid4())

    first_id, created = queue.enqueue_idempotent(
        first_task_id, tenant_id, payload, idempotency_key, request_hash
    )
    assert first_id == first_task_id
    assert created is True

    replay_id, replay_created = queue.enqueue_idempotent(
        str(uuid4()), tenant_id, payload, idempotency_key, request_hash
    )
    assert replay_id == first_task_id
    assert replay_created is False

    changed_hash = hashlib.sha256(b"different task payload").hexdigest()
    with pytest.raises(IdempotencyConflictError):
        queue.enqueue_idempotent(
            str(uuid4()),
            tenant_id,
            {"objective": "different task payload"},
            idempotency_key,
            changed_hash,
        )

    other_task_id = str(uuid4())
    other_id, other_created = queue.enqueue_idempotent(
        other_task_id, other_tenant, payload, idempotency_key, request_hash
    )
    assert other_id == other_task_id
    assert other_created is True

    expected_key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    with queue._connect() as conn:
        stored = conn.execute(
            "SELECT idempotency_key_hash, request_hash FROM aicode_jobs WHERE task_id=%s",
            (first_task_id,),
        ).fetchone()
        assert stored == (expected_key_hash, request_hash)
        conn.execute(
            "DELETE FROM aicode_jobs WHERE tenant_id IN (%s, %s)",
            (tenant_id, other_tenant),
        )


def test_postgres_queue_concurrent_idempotency_returns_one_task_id():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant_id = f"tenant-idempotency-race-{uuid4().hex}"
    idempotency_key = f"concurrent-{uuid4().hex}"
    payload = {"objective": "concurrent stable payload"}
    request_hash = hashlib.sha256(b"concurrent stable payload").hexdigest()

    def submit(_):
        return queue.enqueue_idempotent(
            str(uuid4()), tenant_id, payload, idempotency_key, request_hash
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))

    task_ids = {task_id for task_id, _ in results}
    created_count = sum(1 for _, created in results if created)
    assert len(task_ids) == 1
    assert created_count == 1

    with queue._connect() as conn:
        conn.execute("DELETE FROM aicode_jobs WHERE tenant_id=%s", (tenant_id,))


def test_postgres_queue_cancel_racing_with_success_ack_never_resurrects_task():
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    tenant_id = f"tenant-cancel-race-{uuid4().hex}"
    task_id = str(uuid4())
    assert queue.enqueue(
        task_id, tenant_id, {"objective": "cancel and complete race"},
        max_attempts=1, priority=2147483647,
    )
    claimed = queue.claim("cancel-race-worker", lease_seconds=30)
    assert claimed is not None and claimed.task_id == task_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future = pool.submit(queue.cancel, task_id, tenant_id)
        complete_future = pool.submit(
            queue.complete,
            claimed.id,
            tenant_id,
            "cancel-race-worker",
            claimed.lease_token,
            {"completed": True},
        )
        cancelled = cancel_future.result()
        completed = complete_future.result()

    current = queue.get(task_id, tenant_id)
    assert current is not None
    assert current["status"] in {"cancelled", "succeeded"}
    if current["status"] == "cancelled":
        assert cancelled is True
        assert completed is False
    else:
        assert completed is True
        assert cancelled is False
    assert queue.owns_lease(
        claimed.id, tenant_id, "cancel-race-worker", claimed.lease_token
    ) is False

    with queue._connect() as conn:
        conn.execute("DELETE FROM aicode_jobs WHERE tenant_id=%s", (tenant_id,))


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
