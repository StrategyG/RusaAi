from __future__ import annotations

import pytest

from rusa_agents.queue import (
    PostgresTaskQueue,
    QueueCapacityExceededError,
)


class FakeCursor:
    def __init__(self, row=None, rowcount=1):
        self.row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(
        self,
        *,
        pending_count=0,
        active_count=0,
        existing_idempotency=None,
        task_collision=None,
        inserted_row=(1,),
        update_rowcount=1,
    ):
        self.pending_count = pending_count
        self.active_count = active_count
        self.existing_idempotency = existing_idempotency
        self.task_collision = task_collision
        self.inserted_row = inserted_row
        self.update_rowcount = update_rowcount
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.calls.append((normalized, params))

        if "SELECT 1 FROM public.aicode_jobs" in normalized and "task_id" in normalized:
            return FakeCursor(self.task_collision)
        if (
            "SELECT task_id::text, payload, status" in normalized
            and "idempotency_key" in normalized
        ):
            return FakeCursor(self.existing_idempotency)
        if "SELECT COUNT(*)" in normalized and "status IN" in normalized:
            return FakeCursor((self.pending_count,))
        if "SELECT COUNT(*)" in normalized and "lease_until > NOW()" in normalized:
            return FakeCursor((self.active_count,))
        if "INSERT INTO public.aicode_jobs" in normalized:
            return FakeCursor(self.inserted_row)
        if "UPDATE aicode_jobs" in normalized and "SET status=CASE" in normalized:
            return FakeCursor(rowcount=self.update_rowcount)
        if "WITH candidate AS" in normalized:
            return FakeCursor(None)
        return FakeCursor()


def test_queue_capacity_configuration_is_bounded(monkeypatch):
    monkeypatch.setenv("RUSA_MAX_PENDING_TASKS_PER_TENANT", "100")
    monkeypatch.setenv("RUSA_WORKER_MAX_ACTIVE_JOBS", "1")
    queue = PostgresTaskQueue("postgresql://unused")
    assert queue.max_pending_tasks_per_tenant == 100
    assert queue.max_active_jobs == 1

    with pytest.raises(ValueError, match="RUSA_WORKER_MAX_ACTIVE_JOBS"):
        PostgresTaskQueue("postgresql://unused", max_active_jobs=0)
    with pytest.raises(ValueError, match="RUSA_MAX_PENDING_TASKS_PER_TENANT"):
        PostgresTaskQueue("postgresql://unused", max_pending_tasks_per_tenant=10_001)


def test_enqueue_fails_closed_when_tenant_pending_capacity_is_full(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    queue = PostgresTaskQueue(
        "postgresql://unused", max_pending_tasks_per_tenant=2, max_active_jobs=1
    )
    connection = FakeConnection(pending_count=2)
    monkeypatch.setattr(queue, "_connect", lambda: connection)

    with pytest.raises(QueueCapacityExceededError):
        queue.enqueue("task-new", "tenant-a", {"objective": "new task"})

    assert any("pg_advisory_xact_lock(hashtextextended" in q for q, _ in connection.calls)
    assert not any("INSERT INTO public.aicode_jobs" in q for q, _ in connection.calls)


def test_idempotent_duplicate_is_returned_before_capacity_check(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    queue = PostgresTaskQueue(
        "postgresql://unused", max_pending_tasks_per_tenant=1, max_active_jobs=1
    )
    connection = FakeConnection(
        pending_count=1,
        existing_idempotency=("task-original", {"objective": "same"}, "queued"),
    )
    monkeypatch.setattr(queue, "_connect", lambda: connection)

    result = queue.enqueue_idempotent(
        "task-retry", "tenant-a", {"objective": "same"}, "request-123"
    )

    assert result == ("task-original", False, "queued")
    assert not any("SELECT COUNT(*)" in q and "status IN" in q for q, _ in connection.calls)


def test_claim_does_not_exceed_global_active_job_limit(monkeypatch):
    queue = PostgresTaskQueue(
        "postgresql://unused", max_pending_tasks_per_tenant=10, max_active_jobs=1
    )
    connection = FakeConnection(active_count=1)
    monkeypatch.setattr(queue, "_connect", lambda: connection)

    assert queue.claim("worker-a") is None
    assert any("pg_advisory_xact_lock" in q for q, _ in connection.calls)
    assert not any("WITH candidate AS" in q for q, _ in connection.calls)


def test_retry_delay_is_capped_and_non_integer_values_are_rejected(monkeypatch):
    queue = PostgresTaskQueue("postgresql://unused")
    connection = FakeConnection(update_rowcount=1)
    monkeypatch.setattr(queue, "_connect", lambda: connection)

    assert queue.fail(
        7, "tenant-a", "worker-a", "00000000-0000-4000-8000-000000000007",
        "temporary failure", retry_delay_seconds=99_999,
    )
    update = next(
        params for query, params in connection.calls if "SET status=CASE" in query
    )
    assert update[0] == PostgresTaskQueue.MAX_RETRY_DELAY_SECONDS

    with pytest.raises(ValueError, match="integer"):
        queue.fail(
            7, "tenant-a", "worker-a", "00000000-0000-4000-8000-000000000007",
            "temporary failure", retry_delay_seconds=1.5,
        )


def test_task_attempt_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    queue = PostgresTaskQueue("postgresql://unused")
    with pytest.raises(ValueError, match="between 1 and 10"):
        queue.enqueue("task-too-many-attempts", "tenant-a", {"objective": "x"}, max_attempts=11)
