"""Concurrency and crash-recovery regressions for the existing aicode_jobs queue."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.queue import LeaseLostError, PostgresTaskQueue

DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL integration DSN is not configured")


def make_queue() -> PostgresTaskQueue:
    queue = PostgresTaskQueue(DSN)
    queue.initialize()
    return queue


def expire_lease(job_id: int, tenant_id: str) -> None:
    with psycopg.connect(DSN) as connection:
        connection.execute(
            """UPDATE aicode_jobs
               SET lease_until=NOW() - INTERVAL '1 second'
               WHERE id=%s AND tenant_id=%s""",
            (job_id, tenant_id),
        )


def remove_task(task_id: str, tenant_id: str) -> None:
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "DELETE FROM aicode_jobs WHERE task_id=%s AND tenant_id=%s",
            (task_id, tenant_id),
        )


def claim_specific_task(task_id: str, tenant_id: str, worker_id: str) -> tuple[int, str]:
    token = str(uuid4())
    with psycopg.connect(DSN) as connection:
        row = connection.execute(
            """
            UPDATE aicode_jobs
            SET status='running', attempts=1, lease_owner=%s, lease_token=%s::uuid,
                lease_until=NOW() + INTERVAL '30 seconds'
            WHERE task_id=%s AND tenant_id=%s AND status='queued'
            RETURNING id
            """,
            (worker_id, token, task_id, tenant_id),
        ).fetchone()
    if row is None:
        raise AssertionError("test task could not be claimed")
    return row[0], token


def test_crashed_worker_lease_is_reclaimed_and_old_token_is_fenced():
    queue = make_queue()
    tenant = f"queue-crash-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "crash recovery"}, priority=2**31 - 1)
        crashed = queue.claim("crashed-worker", lease_seconds=30)
        assert crashed and crashed.task_id == task_id
        expire_lease(crashed.id, tenant)

        recovered = queue.claim("recovery-worker", lease_seconds=30)
        assert recovered and recovered.task_id == task_id
        assert recovered.lease_token != crashed.lease_token
        assert not queue.complete(
            crashed.id, tenant, "crashed-worker", crashed.lease_token,
            {"outcome": "stale"},
        )
        assert queue.owns_lease(
            recovered.id, tenant, "recovery-worker", recovered.lease_token
        )
    finally:
        remove_task(task_id, tenant)


def test_concurrent_duplicate_enqueue_is_idempotent():
    queue = make_queue()
    tenant = f"queue-idempotency-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(
                lambda _: queue.enqueue(task_id, tenant, {"objective": "same task"}),
                range(16),
            ))
        assert sum(outcomes) == 1
        assert queue.get(task_id, tenant)["status"] == "queued"
    finally:
        remove_task(task_id, tenant)


def test_two_workers_cannot_claim_the_same_job_concurrently():
    queue = make_queue()
    tenant = f"queue-single-claim-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "exclusive claim"}, priority=2**31 - 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(
                lambda worker: queue.claim(worker, lease_seconds=30),
                ("claim-worker-a", "claim-worker-b"),
            ))
        owned = [item for item in claims if item is not None and item.task_id == task_id]
        assert len(owned) == 1
        assert queue.get(task_id, tenant)["attempts"] == 1
    finally:
        remove_task(task_id, tenant)


def test_cancel_racing_with_successful_completion_is_linearizable():
    queue = make_queue()
    tenant = f"queue-cancel-race-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "cancel race"}, priority=2**31 - 1)
        job = queue.claim("cancel-race-worker", lease_seconds=30)
        assert job and job.task_id == task_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled = pool.submit(queue.cancel, task_id, tenant)
            completed = pool.submit(
                queue.complete, job.id, tenant, "cancel-race-worker",
                job.lease_token, {"outcome": "success"},
            )
            cancel_result = cancelled.result()
            complete_result = completed.result()

        current = queue.get(task_id, tenant)
        assert current is not None
        assert current["status"] in {"cancelled", "succeeded"}
        assert (cancel_result, complete_result) in {(True, False), (False, True)}
        assert not queue.owns_lease(job.id, tenant, "cancel-race-worker", job.lease_token)
    finally:
        remove_task(task_id, tenant)


def test_expired_worker_attempt_limit_moves_job_to_failed():
    queue = make_queue()
    tenant = f"queue-retry-limit-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(
            task_id, tenant, {"objective": "attempt limit"},
            max_attempts=2, priority=2**31 - 1,
        )
        first = queue.claim("retry-worker-a", lease_seconds=30)
        assert first and first.task_id == task_id
        expire_lease(first.id, tenant)
        second = queue.claim("retry-worker-b", lease_seconds=30)
        assert second and second.task_id == task_id
        expire_lease(second.id, tenant)

        third = queue.claim("retry-worker-c", lease_seconds=30)
        assert third is None or third.task_id != task_id
        current = queue.get(task_id, tenant)
        assert current is not None
        assert current["status"] == "failed"
        assert current["attempts"] == 2
    finally:
        remove_task(task_id, tenant)


def test_lease_bound_operation_refuses_expired_lease_before_checkpoint():
    queue = make_queue()
    tenant = f"queue-checkpoint-stale-{uuid4().hex}"
    task_id = str(uuid4())
    called = []
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "stale checkpoint"})
        job = queue.claim("checkpoint-stale-worker", lease_seconds=30)
        assert job and job.task_id == task_id
        expire_lease(job.id, tenant)

        with pytest.raises(LeaseLostError, match="active task lease"):
            queue.with_lease_owned(
                job.id,
                tenant,
                "checkpoint-stale-worker",
                job.lease_token,
                lambda: called.append("checkpointed"),
            )
        assert called == []
    finally:
        remove_task(task_id, tenant)


def test_replacement_worker_cannot_reclaim_during_lease_bound_checkpoint():
    queue = make_queue()
    tenant = f"queue-checkpoint-fence-{uuid4().hex}"
    task_id = str(uuid4())
    checkpointing = Event()
    release_checkpoint = Event()
    expiry_started = Event()

    def expire_while_locked(job_id: int) -> None:
        with psycopg.connect(DSN) as connection:
            expiry_started.set()
            connection.execute(
                "UPDATE aicode_jobs SET lease_until=NOW() - INTERVAL '1 second' "
                "WHERE id=%s AND tenant_id=%s",
                (job_id, tenant),
            )

    def bounded_checkpoint() -> str:
        checkpointing.set()
        if not release_checkpoint.wait(timeout=10):
            raise TimeoutError("test checkpoint was not released")
        return "checkpoint-saved"

    try:
        assert queue.enqueue(task_id, tenant, {"objective": "fenced checkpoint"})
        job = queue.claim("checkpoint-fence-worker", lease_seconds=30)
        assert job and job.task_id == task_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            checkpoint = pool.submit(
                queue.with_lease_owned,
                job.id,
                tenant,
                "checkpoint-fence-worker",
                job.lease_token,
                bounded_checkpoint,
                lease_seconds=30,
            )
            assert checkpointing.wait(timeout=5)

            expiry = pool.submit(expire_while_locked, job.id)
            assert expiry_started.wait(timeout=5)
            time.sleep(0.1)
            assert not expiry.done(), "lease row was not locked during checkpoint"

            assert queue.claim("replacement-checkpoint-worker", lease_seconds=30) is None
            release_checkpoint.set()
            assert checkpoint.result(timeout=10) == "checkpoint-saved"
            expiry.result(timeout=10)

        replacement = queue.claim("replacement-checkpoint-worker", lease_seconds=30)
        assert replacement and replacement.task_id == task_id
        assert replacement.lease_token != job.lease_token
    finally:
        release_checkpoint.set()
        remove_task(task_id, tenant)


def test_publication_refuses_an_expired_or_replaced_lease():
    queue = make_queue()
    tenant = f"queue-publish-stale-{uuid4().hex}"
    task_id = str(uuid4())
    called = []
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "stale publication"})
        job_id, lease_token = claim_specific_task(task_id, tenant, "publish-stale-worker")
        expire_lease(job_id, tenant)

        with pytest.raises(LeaseLostError, match="active task lease"):
            queue.publish_if_lease_owned(
                job_id,
                tenant,
                "publish-stale-worker",
                lease_token,
                lambda: called.append("published"),
            )
        assert called == []
    finally:
        remove_task(task_id, tenant)


def test_replacement_worker_cannot_reclaim_during_repository_publication():
    queue = make_queue()
    tenant = f"queue-publish-fence-{uuid4().hex}"
    task_id = str(uuid4())
    publishing = Event()
    release_publish = Event()
    expiry_started = Event()

    def expire_while_locked(job_id: int) -> None:
        with psycopg.connect(DSN) as connection:
            expiry_started.set()
            connection.execute(
                "UPDATE aicode_jobs SET lease_until=NOW() - INTERVAL '1 second' "
                "WHERE id=%s AND tenant_id=%s",
                (job_id, tenant),
            )

    def bounded_publish() -> str:
        publishing.set()
        if not release_publish.wait(timeout=10):
            raise TimeoutError("test publisher was not released")
        return "a" * 40

    try:
        assert queue.enqueue(task_id, tenant, {"objective": "fenced publication"})
        job_id, lease_token = claim_specific_task(task_id, tenant, "publish-fence-worker")

        with ThreadPoolExecutor(max_workers=2) as pool:
            publication = pool.submit(
                queue.publish_if_lease_owned,
                job_id,
                tenant,
                "publish-fence-worker",
                lease_token,
                bounded_publish,
                lease_seconds=900,
            )
            assert publishing.wait(timeout=5)

            expiry = pool.submit(expire_while_locked, job_id)
            assert expiry_started.wait(timeout=5)
            time.sleep(0.1)
            assert not expiry.done(), "lease row was not locked during publication"

            release_publish.set()
            assert publication.result(timeout=10) == "a" * 40
            expiry.result(timeout=10)

        replacement_token = str(uuid4())
        with psycopg.connect(DSN) as connection:
            replacement = connection.execute(
                """
                UPDATE aicode_jobs
                SET lease_owner='replacement-worker', lease_token=%s::uuid,
                    lease_until=NOW() + INTERVAL '30 seconds', attempts=attempts+1
                WHERE id=%s AND tenant_id=%s AND status='running'
                  AND lease_until < NOW()
                RETURNING lease_token::text
                """,
                (replacement_token, job_id, tenant),
            ).fetchone()
        assert replacement and replacement[0] != lease_token
    finally:
        release_publish.set()
        remove_task(task_id, tenant)
