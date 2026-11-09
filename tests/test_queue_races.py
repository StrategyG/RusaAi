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
        crashed = queue.claim("crashed-worker", lease_seconds=30, tenant_id=tenant)
        assert crashed and crashed.task_id == task_id
        expire_lease(crashed.id, tenant)

        recovered = queue.claim("recovery-worker", lease_seconds=30, tenant_id=tenant)
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
                lambda worker: queue.claim(worker, lease_seconds=30, tenant_id=tenant),
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
        job = queue.claim("cancel-race-worker", lease_seconds=30, tenant_id=tenant)
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
        assert (cancel_result, complete_result) in {(True, False), (False, True)}
        if cancel_result:
            assert current["status"] == "cancel_requested"
            assert queue.heartbeat_state(
                job.id, tenant, "cancel-race-worker", job.lease_token, 30
            ) == "cancel_requested"
            assert queue.finish_cancelled(
                job.id, tenant, "cancel-race-worker", job.lease_token,
                side_effect="NONE",
            )
            assert queue.get(task_id, tenant)["status"] == "cancelled"
        else:
            assert current["status"] == "succeeded"
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
        first = queue.claim("retry-worker-a", lease_seconds=30, tenant_id=tenant)
        assert first and first.task_id == task_id
        expire_lease(first.id, tenant)
        second = queue.claim("retry-worker-b", lease_seconds=30, tenant_id=tenant)
        assert second and second.task_id == task_id
        expire_lease(second.id, tenant)

        third = queue.claim("retry-worker-c", lease_seconds=30, tenant_id=tenant)
        assert third is None or third.task_id != task_id
        current = queue.get(task_id, tenant)
        assert current is not None
        assert current["status"] == "failed"
        assert current["attempts"] == 2
    finally:
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



def test_running_cancel_requires_worker_acknowledgement_and_preserves_lease():
    queue = make_queue()
    tenant = f"queue-cooperative-cancel-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "cooperative cancellation"})
        job = queue.claim("cooperative-cancel-worker", lease_seconds=30, tenant_id=tenant)
        assert job and job.task_id == task_id
        assert queue.cancel(task_id, tenant)
        current = queue.get(task_id, tenant)
        assert current and current["status"] == "cancel_requested"
        assert queue.lease_status(job.id, tenant, "cooperative-cancel-worker", job.lease_token) == "cancel_requested"
        assert queue.finish_cancelled(
            job.id, tenant, "cooperative-cancel-worker", job.lease_token,
            side_effect="NONE",
        )
        current = queue.get(task_id, tenant)
        assert current and current["status"] == "cancelled"
        assert current["result"]["side_effect"] == "NONE"
    finally:
        remove_task(task_id, tenant)


def test_cancel_with_unknown_side_effect_requires_reconciliation():
    queue = make_queue()
    tenant = f"queue-cancel-reconcile-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "ambiguous side effect"})
        job = queue.claim("cancel-reconcile-worker", lease_seconds=30, tenant_id=tenant)
        assert job and job.task_id == task_id
        assert queue.cancel(task_id, tenant)
        assert queue.finish_cancelled(
            job.id, tenant, "cancel-reconcile-worker", job.lease_token,
            side_effect="UNKNOWN",
            result={"operation": "pull_request.create"},
        )
        current = queue.get(task_id, tenant)
        assert current and current["status"] == "awaiting_approval"
        assert current["result"]["cancel_reconciliation_required"] is True
        assert queue.retry(task_id, tenant) is False
        assert queue.reconcile_cancellation(
            task_id,
            tenant,
            side_effect="APPLIED",
            evidence="Verified the remote branch and draft PR state in GitHub.",
        )
        reconciled = queue.get(task_id, tenant)
        assert reconciled["status"] == "awaiting_approval"
        assert reconciled["result"]["cancel_reconciliation_required"] is False
        assert reconciled["result"]["reconciliation"]["outcome"] == "APPLIED"
        assert queue.retry(task_id, tenant) is True
        retried = queue.get(task_id, tenant)
        assert retried["status"] == "queued"
        assert retried["result"]["reconciliation"]["evidence"].startswith("Verified the remote")
    finally:
        remove_task(task_id, tenant)


def test_unacknowledged_cancel_is_quarantined_after_lease_expiry():
    queue = make_queue()
    tenant = f"queue-cancel-expired-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "worker crash during cancellation"})
        job = queue.claim("cancel-expiry-worker", lease_seconds=30, tenant_id=tenant)
        assert job and job.task_id == task_id
        assert queue.cancel(task_id, tenant)
        expire_lease(job.id, tenant)
        queue.claim("recovery-worker", lease_seconds=30, tenant_id=tenant)
        current = queue.get(task_id, tenant)
        assert current and current["status"] == "awaiting_approval"
        assert current["result"]["side_effect"] == "UNKNOWN"
        assert current["result"]["cancel_reconciliation_required"] is True
    finally:
        remove_task(task_id, tenant)


def test_queued_cancel_is_immediate_without_claiming_a_lease():
    queue = make_queue()
    tenant = f"queue-cancel-queued-{uuid4().hex}"
    task_id = str(uuid4())
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "cancel before execution"})
        assert queue.cancel(task_id, tenant)
        current = queue.get(task_id, tenant)
        assert current and current["status"] == "cancelled"
        assert current["attempts"] == 0
    finally:
        remove_task(task_id, tenant)



def test_cancellation_reconciliation_rejects_credential_bearing_evidence():
    queue = make_queue()
    tenant = f"queue-cancel-reconcile-secret-{uuid4().hex}"
    task_id = str(uuid4())
    token = "ghp_" + "Z" * 36
    try:
        assert queue.enqueue(task_id, tenant, {"objective": "secret in reconciliation evidence"})
        job = queue.claim("cancel-reconcile-secret-worker", lease_seconds=30, tenant_id=tenant)
        assert job and job.task_id == task_id
        assert queue.cancel(task_id, tenant)
        assert queue.finish_cancelled(
            job.id, tenant, "cancel-reconcile-secret-worker", job.lease_token,
            side_effect="UNKNOWN",
        )
        with pytest.raises(ValueError, match="Reconciliation evidence"):
            queue.reconcile_cancellation(
                task_id, tenant, side_effect="NONE",
                evidence="Credential " + token,
            )
        assert queue.retry(task_id, tenant) is False
    finally:
        remove_task(task_id, tenant)
