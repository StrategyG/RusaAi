from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

import pytest

from rusa_agents.queue import Job, PostgresTaskQueue


class FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None = None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection(AbstractContextManager):
    def __init__(self, *, select_row=None, update_row=(1,)):
        self.select_row = select_row
        self.update_row = update_row
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query: str, params=None):
        self.calls.append((query, params))
        if "SELECT id, result, status" in query:
            return FakeCursor(self.select_row)
        if "UPDATE public.aicode_jobs" in query:
            return FakeCursor(self.update_row)
        return FakeCursor()


def test_queue_claim_exposes_durable_approval_result(monkeypatch):
    approval = {
        "plan_fingerprint": "a" * 64,
        "approved_node_ids": ["node-risk"],
        "approved_by": "api:tenant-admin",
        "rationale": "Reviewed",
        "approved_at": "2026-11-13T09:00:00Z",
    }
    result = {"plan_fingerprint": "a" * 64, "approval": approval}
    connection = FakeConnection(
        select_row=(
            10, "task-10", "tenant-a", {"execution_mode": "agent_runtime_v1"},
            1, 3, "worker-a", "00000000-0000-4000-8000-000000000010", result,
        )
    )
    queue = PostgresTaskQueue("postgresql://unused")
    monkeypatch.setattr(queue, "_connect", lambda: connection)

    job = queue.claim("worker-a", lease_seconds=180)

    assert isinstance(job, Job)
    assert job.result == result
    assert job.result["approval"]["approved_node_ids"] == ["node-risk"]


def test_queue_approval_updates_only_matching_pending_nodes(monkeypatch):
    fingerprint = "a" * 64
    pending_result = {
        "plan_fingerprint": fingerprint,
        "node_results": [
            {"task_id": "node-risk", "status": "needs_approval"},
            {"task_id": "node-done", "status": "completed"},
        ],
    }
    connection = FakeConnection(
        select_row=(10, pending_result, "awaiting_approval"),
        update_row=(10,),
    )
    queue = PostgresTaskQueue("postgresql://unused")
    monkeypatch.setattr(queue, "_connect", lambda: connection)
    monkeypatch.setenv("ENVIRONMENT", "development")

    approved = queue.approve(
        "task-10",
        "tenant-a",
        fingerprint,
        ["node-risk"],
        "Reviewed and approved",
        audit_actor="api:tenant-admin",
    )

    assert approved is True
    update_calls = [call for call in connection.calls if "UPDATE public.aicode_jobs" in call[0]]
    assert len(update_calls) == 1
    assert "jsonb_set" in update_calls[0][0]
    audit_calls = [call for call in connection.calls if "INSERT INTO public.aicode_task_policy_events" in call[0]]
    assert len(audit_calls) == 1
    assert audit_calls[0][1][-2:] == ("task.approve", "allow")


@pytest.mark.parametrize(
    ("fingerprint", "node_ids", "status"),
    [
        ("b" * 64, ["node-risk"], "awaiting_approval"),
        ("a" * 64, ["node-done"], "awaiting_approval"),
        ("a" * 64, ["node-risk"], "queued"),
    ],
)
def test_queue_approval_rejects_stale_or_ineligible_state(
    monkeypatch, fingerprint, node_ids, status
):
    result = {
        "plan_fingerprint": "a" * 64,
        "node_results": [
            {"task_id": "node-risk", "status": "needs_approval"},
            {"task_id": "node-done", "status": "completed"},
        ],
    }
    connection = FakeConnection(select_row=(10, result, status))
    queue = PostgresTaskQueue("postgresql://unused")
    monkeypatch.setattr(queue, "_connect", lambda: connection)
    monkeypatch.setenv("ENVIRONMENT", "development")

    approved = queue.approve(
        "task-10",
        "tenant-a",
        fingerprint,
        node_ids,
        "Approval request",
        audit_actor="api:tenant-admin",
    )

    assert approved is False
    assert not any("UPDATE public.aicode_jobs" in call[0] for call in connection.calls)
    audit_calls = [call for call in connection.calls if "INSERT INTO public.aicode_task_policy_events" in call[0]]
    assert len(audit_calls) == 1
    assert audit_calls[0][1][-2:] == ("task.approve", "deny")


def test_queue_approval_rejects_duplicate_node_ids_before_database_access(monkeypatch):
    queue = PostgresTaskQueue("postgresql://unused")
    monkeypatch.setenv("ENVIRONMENT", "development")

    with pytest.raises(ValueError, match="invalid"):
        queue.approve(
            "task-10",
            "tenant-a",
            "a" * 64,
            ["node-risk", "node-risk"],
            "Duplicate approval",
            audit_actor="api:tenant-admin",
        )
