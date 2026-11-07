"""PostgreSQL integration tests for durable DevOps communication primitives."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.store import (
    OutboxLeaseLostError,
    PostgresInbox,
    PostgresOutboxRelay,
    PostgresWorkflowStore,
    WorkflowIdempotencyConflictError,
)

DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="RUSA_DATABASE_URL is not configured")


def _event(tenant_id: str, run_id: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "type": "task.request",
        "message_id": str(uuid4()),
        "tenant_id": tenant_id,
        "run_id": run_id or str(uuid4()),
        "task_id": str(uuid4()),
        "attempt": 1,
        "correlation_id": str(uuid4()),
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "producer": "rusa_devops_orchestrator",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "capability": "plan",
            "input_ref": "artifact://test/input",
            "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            "idempotency_key": "test-run/task/1",
            "priority": "P2",
        },
    }


def _task_contract(tenant_id: str, event: dict) -> dict:
    return {
        "schema_version": 1,
        "task_id": event["task_id"],
        "workflow_id": event["run_id"],
        "tenant_id": tenant_id,
        "agent_id": "rusa_plan_agent",
        "stage": "plan",
        "objective": "Verify durable workflow persistence",
        "input": {"user_request": "Create a durable workflow integration test"},
        "constraints": {
            "allowed_tools": [],
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": [],
            "approval_required": False,
            "approval_scope": [],
            "budget": {
                "max_cost_usd": 0,
                "max_tool_calls": 0,
                "max_runtime_seconds": 600,
            },
        },
        "attempt": event["attempt"],
        "deadline_at": event["payload"]["deadline_at"],
        "audit": {
            "created_at": event["occurred_at"],
            "requested_by": "pytest",
            "policy_version": "test-policy-v1",
            "traceparent": event["traceparent"],
        },
    }


def _cleanup(tenant_id: str) -> None:
    import psycopg

    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM public.devops_task_rpc_receipts WHERE tenant_id=%s", (tenant_id,))
        conn.execute("DELETE FROM public.devops_run_cancellations WHERE tenant_id=%s", (tenant_id,))
        conn.execute("DELETE FROM public.devops_event_outbox WHERE tenant_id=%s", (tenant_id,))
        conn.execute("DELETE FROM public.devops_tasks WHERE tenant_id=%s", (tenant_id,))
        conn.execute("DELETE FROM public.devops_workflows WHERE tenant_id=%s", (tenant_id,))


def test_workflow_and_initial_command_are_committed_atomically_and_idempotently():
    tenant_id = f"test-{uuid4()}"
    store = PostgresWorkflowStore(DSN)
    request = {"objective": "integration-test", "scope": ["read-only"]}
    event = _event(tenant_id)
    initial_task = _task_contract(tenant_id, event)
    try:
        run_id, created = store.submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="same-request",
            request=request,
            initial_task=initial_task,
            initial_event=event,
        )
        replay_id, replay_created = store.submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="same-request",
            request=request,
            initial_task=initial_task,
            initial_event=event,
        )

        assert created is True
        assert replay_created is False
        assert replay_id == run_id

        with store._connect() as conn:
            store._set_tenant(conn, tenant_id)
            workflow_count = conn.execute(
                "SELECT count(*) FROM public.devops_workflows WHERE tenant_id=%s",
                (tenant_id,),
            ).fetchone()[0]
            task_count = conn.execute(
                "SELECT count(*) FROM public.devops_tasks WHERE tenant_id=%s",
                (tenant_id,),
            ).fetchone()[0]
            outbox_count = conn.execute(
                "SELECT count(*) FROM public.devops_event_outbox WHERE tenant_id=%s",
                (tenant_id,),
            ).fetchone()[0]
        assert workflow_count == 1
        assert task_count == 1
        assert outbox_count == 1
    finally:
        _cleanup(tenant_id)


def test_idempotency_key_with_different_request_body_is_rejected():
    tenant_id = f"test-{uuid4()}"
    store = PostgresWorkflowStore(DSN)
    try:
        first_event = _event(tenant_id)
        store.submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="same-key",
            request={"objective": "first"},
            initial_task=_task_contract(tenant_id, first_event),
            initial_event=first_event,
        )
        second_event = _event(tenant_id)
        with pytest.raises(WorkflowIdempotencyConflictError):
            store.submit_workflow(
                tenant_id=tenant_id,
                idempotency_key="same-key",
                request={"objective": "different"},
                initial_task=_task_contract(tenant_id, second_event),
                initial_event=second_event,
            )
    finally:
        _cleanup(tenant_id)


def test_outbox_claim_requires_lease_token_to_mark_published():
    tenant_id = f"test-{uuid4()}"
    store = PostgresWorkflowStore(DSN)
    relay = PostgresOutboxRelay(DSN)
    try:
        event = _event(tenant_id)
        store.submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="relay-test",
            request={"objective": "relay"},
            initial_task=_task_contract(tenant_id, event),
            initial_event=event,
        )
        claimed = relay.claim(destination="commands", limit=1)
        assert len(claimed) == 1
        assert claimed[0]["tenant_id"] == tenant_id
        relay.mark_published(
            outbox_id=claimed[0]["outbox_id"],
            lease_token=claimed[0]["lease_token"],
        )
        assert relay.claim(destination="commands", limit=1) == []
    finally:
        _cleanup(tenant_id)


def test_inbox_deduplicates_replay_and_applies_handler_once():
    tenant_id = f"test-{uuid4()}"
    inbox = PostgresInbox(DSN)
    event = _event(tenant_id)
    calls = []

    def handler(conn, received_event):
        calls.append(received_event["message_id"])

    try:
        assert inbox.process_once(
            tenant_id=tenant_id,
            consumer_id="projection-test",
            event=event,
            handler=handler,
        ) is True
        assert inbox.process_once(
            tenant_id=tenant_id,
            consumer_id="projection-test",
            event=event,
            handler=handler,
        ) is False
        assert calls == [event["message_id"]]
    finally:
        _cleanup(tenant_id)


def test_workflow_store_rejects_event_from_another_tenant():
    tenant_id = f"test-{uuid4()}"
    event = _event(f"other-{uuid4()}")
    store = PostgresWorkflowStore(DSN)
    with pytest.raises(ValueError, match="tenant_id"):
        store.submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="tenant-mismatch",
            request={"objective": "must-not-persist"},
            initial_task=_task_contract(event["tenant_id"], event),
            initial_event=event,
        )

def test_inbox_rejects_same_message_id_with_different_envelope():
    tenant_id = f"test-{uuid4()}"
    inbox = PostgresInbox(DSN)
    event = _event(tenant_id)
    conflicting = {**event, "payload": {**event["payload"], "priority": "P1"}}
    try:
        assert inbox.process_once(
            tenant_id=tenant_id,
            consumer_id="conflict-test",
            event=event,
            handler=lambda conn, received: None,
        ) is True
        with pytest.raises(ValueError, match="different event envelope"):
            inbox.process_once(
                tenant_id=tenant_id,
                consumer_id="conflict-test",
                event=conflicting,
                handler=lambda conn, received: None,
            )
    finally:
        _cleanup(tenant_id)


def test_outbox_stale_lease_cannot_settle_a_row():
    tenant_id = f"test-{uuid4()}"
    store = PostgresWorkflowStore(DSN)
    relay = PostgresOutboxRelay(DSN)
    try:
        event = _event(tenant_id)
        store.submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="stale-lease-test",
            request={"objective": "fencing"},
            initial_task=_task_contract(tenant_id, event),
            initial_event=event,
        )
        claimed = relay.claim(destination="commands", limit=1)
        assert len(claimed) == 1
        with pytest.raises(OutboxLeaseLostError, match="lease is no longer owned"):
            relay.mark_published(
                outbox_id=claimed[0]["outbox_id"],
                lease_token=str(uuid4()),
            )
        relay.mark_published(
            outbox_id=claimed[0]["outbox_id"],
            lease_token=claimed[0]["lease_token"],
        )
    finally:
        _cleanup(tenant_id)

def test_durable_tables_force_rls_and_have_separate_relay_role():
    import psycopg

    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid=c.relnamespace
            WHERE n.nspname='public'
              AND c.relname = ANY(%s)
            """,
            ([
                "devops_workflows",
                "devops_tasks",
                "devops_event_outbox",
                "devops_event_inbox",
                "devops_run_cancellations",
                "devops_task_rpc_receipts",
            ],),
        ).fetchall()
        roles = conn.execute(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
            FROM pg_roles WHERE rolname = ANY(%s)
            """,
            (["rusa_api_runtime", "rusa_worker_runtime", "rusa_relay_runtime"],),
        ).fetchall()

    assert len(rows) == 6
    assert all(enabled and forced for _, enabled, forced in rows)
    assert {name for name, *_ in roles} == {
        "rusa_api_runtime", "rusa_worker_runtime", "rusa_relay_runtime"
    }
    assert all(not can_login and not superuser and not bypass for _, can_login, superuser, bypass in roles)


def test_task_and_outbox_foreign_keys_prevent_cross_tenant_run_linkage():
    import psycopg
    from psycopg.errors import ForeignKeyViolation

    tenant_id = f"test-{uuid4()}"
    other_tenant = f"other-{uuid4()}"
    event = _event(tenant_id)
    try:
        run_id, _ = PostgresWorkflowStore(DSN).submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="tenant-fk",
            request={"objective": "tenant-link"},
            initial_task=_task_contract(tenant_id, event),
            initial_event=event,
        )
        with pytest.raises(ForeignKeyViolation):
            with psycopg.connect(DSN) as conn:
                conn.execute(
                    """
                    INSERT INTO public.devops_tasks
                        (task_id, run_id, tenant_id, agent_id, stage, task)
                    VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)
                    """,
                    (str(uuid4()), run_id, other_tenant, "rusa_plan_agent", "plan"),
                )
    finally:
        _cleanup(tenant_id)

def test_append_outbox_event_shares_the_callers_transaction():
    tenant_id = f"test-{uuid4()}"
    store = PostgresWorkflowStore(DSN)
    initial = _event(tenant_id)
    try:
        run_id, _ = store.submit_workflow(
            tenant_id=tenant_id,
            idempotency_key="shared-transaction",
            request={"objective": "atomic-state-and-event"},
            initial_task=_task_contract(tenant_id, initial),
            initial_event=initial,
        )
        progress = _event(tenant_id, run_id)
        progress["task_id"] = initial["task_id"]
        progress["type"] = "task.progress"
        progress["payload"] = {
            "lease_epoch": 1,
            "state_version": 2,
            "percent": 50,
            "stage": "code",
        }

        with pytest.raises(RuntimeError, match="force transaction rollback"):
            with store._connect() as conn:
                store._set_tenant(conn, tenant_id)
                conn.execute(
                    """
                    UPDATE public.devops_workflows
                    SET status='running', state_version=state_version + 1
                    WHERE tenant_id=%s AND run_id=%s
                    """,
                    (tenant_id, run_id),
                )
                store.append_outbox_event(
                    conn=conn,
                    tenant_id=tenant_id,
                    event=progress,
                    destination="events",
                    aggregate_version=2,
                )
                raise RuntimeError("force transaction rollback")

        with store._connect() as conn:
            store._set_tenant(conn, tenant_id)
            status, state_version = conn.execute(
                """
                SELECT status, state_version
                FROM public.devops_workflows
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()
            outbox_count = conn.execute(
                """
                SELECT count(*) FROM public.devops_event_outbox
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
        assert status == "queued"
        assert state_version == 1
        assert outbox_count == 1
    finally:
        _cleanup(tenant_id)

