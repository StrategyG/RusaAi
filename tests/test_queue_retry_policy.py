"""Unit tests for the AICode retry state policy without a PostgreSQL dependency."""

import pytest

from rusa_agents.queue import PostgresTaskQueue


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, current_status):
        self.current_status = current_status
        self.retry_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        if "set_config" in sql:
            return FakeCursor(None)
        if "UPDATE aicode_jobs" in sql:
            self.retry_sql = "".join(sql.split())
            eligible = (
                self.current_status == "failed"
                and "status='failed'" in self.retry_sql
            )
            return FakeCursor((1,) if eligible else None)
        raise AssertionError(f"Unexpected queue SQL: {sql}")


@pytest.mark.parametrize(
    ("current_status", "expected"),
    [
        ("failed", True),
        ("awaiting_approval", False),
        ("queued", False),
        ("running", False),
        ("succeeded", False),
        ("cancelled", False),
    ],
)
def test_explicit_retry_only_requeues_failed_tasks(monkeypatch, current_status, expected):
    monkeypatch.setenv("ENVIRONMENT", "development")
    connection = FakeConnection(current_status)
    queue = PostgresTaskQueue("postgresql://unused-in-unit-test")
    monkeypatch.setattr(queue, "_connect", lambda: connection)

    assert queue.retry(
        "00000000-0000-4000-8000-000000000001",
        "tenant-a",
    ) is expected
    assert "status='failed'" in connection.retry_sql
    assert "awaiting_approval" not in connection.retry_sql
