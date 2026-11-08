"""PostgreSQL integration tests for fenced DevOps control-plane operations."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.control_store import (
    PostgresWorkflowControlStore,
    RpcReplayConflictError,
)
from rusa_agents.devops.rabbitmq import command_route
from rusa_agents.devops.store import PostgresWorkflowStore

DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="RUSA_DATABASE_URL is not configured")

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _initial_event(tenant_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "type": "task.request",
        "message_id": str(uuid4()),
        "tenant_id": tenant_id,
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "attempt": 1,
        "correlation_id": str(uuid4()),
        "traceparent": TRACEPARENT,
        "producer": "rusa_devops_orchestrator",
        "occurred_at": now.isoformat(),
        "payload": {
            "capability": "plan",
            "input_ref": "artifact://test/plan-input",
            "deadline_at": (now + timedelta(minutes=10)).isoformat(),
            "idempotency_key": str(uuid4()),
            "priority": "P2",
        },
    }


def _initial_task(tenant_id: str, event: dict) -> dict:
    return {
        "schema_version": 1,
        "task_id": event["task_id"],
        "workflow_id": event["run_id"],
        "tenant_id": tenant_id,
        "agent_id": "rusa_plan_agent",
        "stage": "plan",
        "objective": "Verify fenced task execution",
        "input": {"user_request": "Run the durable workflow integration test"},
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
            "traceparent": TRACEPARENT,
        },
    }


def _create_run(tenant_id: str | None = None):
    tenant_id = tenant_id or f"control-{uuid4()}"
    event = _initial_event(tenant_id)
    task = _initial_task(tenant_id, event)
    run_id, created = PostgresWorkflowStore(DSN).submit_workflow(
        tenant_id=tenant_id,
        idempotency_key=str(uuid4()),
        request={"objective": "fenced control-plane test"},
        initial_task=task,
        initial_event=event,
    )
    assert created is True
    return tenant_id, run_id, event, task


def _control_store():
    return PostgresWorkflowControlStore(
        api_dsn=DSN,
        worker_dsn=DSN,
        lease_seconds=30,
        statement_timeout_ms=2000,
    )


def _claim(store, tenant_id, run_id, task, claim_id="worker-instance-1"):
    return store.claim_task(
        tenant_id=tenant_id,
        run_id=run_id,
        task_id=task["task_id"],
        attempt=task["attempt"],
        worker_id=task["agent_id"],
        claim_id=claim_id,
        traceparent=TRACEPARENT,
    )


def _cleanup(tenant_id: str):
    import psycopg

    with psycopg.connect(DSN) as conn:
        conn.execute(
            "DELETE FROM public.devops_task_rpc_receipts WHERE tenant_id=%s",
            (tenant_id,),
        )
        conn.execute(
            "DELETE FROM public.devops_run_cancellations WHERE tenant_id=%s",
            (tenant_id,),
        )
        conn.execute(
            "DELETE FROM public.devops_event_outbox WHERE tenant_id=%s",
            (tenant_id,),
        )
        conn.execute(
            "DELETE FROM public.devops_tasks WHERE tenant_id=%s",
            (tenant_id,),
        )
        conn.execute(
            "DELETE FROM public.devops_workflows WHERE tenant_id=%s",
            (tenant_id,),
        )


def _result(task: dict, *, status="passed") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "schema_version": 1,
        "task_id": task["task_id"],
        "workflow_id": task["workflow_id"],
        "tenant_id": task["tenant_id"],
        "agent_id": task["agent_id"],
        "stage": task["stage"],
        "attempt": task["attempt"],
        "status": status,
        "output": {
            "summary": "The task result is ready for orchestration.",
            "artifact_refs": [],
            "evidence_refs": ["artifact://test/evidence/plan"],
            "findings": [],
        },
        "audit": {
            "started_at": task["audit"]["created_at"],
            "completed_at": now,
            "policy_version": "test-policy-v1",
        },
    }
    if status == "passed":
        result["handoff"] = {
            "next_agent": "rusa_code_agent",
            "event": "plan.completed",
        }
    return result


def test_claim_is_idempotent_only_for_the_same_worker_instance():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        first = _claim(store, tenant_id, run_id, task, "worker-instance-a")
        replay = _claim(store, tenant_id, run_id, task, "worker-instance-a")
        competing = _claim(store, tenant_id, run_id, task, "worker-instance-b")

        assert first["accepted"] is True
        assert first["lease_epoch"] == 1
        assert replay["accepted"] is True
        assert replay["lease_epoch"] == first["lease_epoch"]
        assert replay["state_version"] == first["state_version"]
        assert competing["accepted"] is False
        assert competing["lease_epoch"] == first["lease_epoch"]
    finally:
        _cleanup(tenant_id)


def test_heartbeat_requires_current_claim_id_and_never_resurrects_expired_lease():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        claimed = _claim(store, tenant_id, run_id, task, "worker-instance-a")
        wrong_instance = store.heartbeat_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
            attempt=1,
            lease_epoch=claimed["lease_epoch"],
            worker_id=task["agent_id"],
            claim_id="worker-instance-b",
            traceparent=TRACEPARENT,
        )
        assert wrong_instance["accepted"] is False

        valid = store.heartbeat_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
            attempt=1,
            lease_epoch=claimed["lease_epoch"],
            worker_id=task["agent_id"],
            claim_id="worker-instance-a",
            traceparent=TRACEPARENT,
        )
        assert valid["accepted"] is True
        assert valid["state_version"] == claimed["state_version"]

        with store._connect(DSN) as conn:
            conn.execute(
                """
                UPDATE public.devops_tasks
                SET lease_until=clock_timestamp() - INTERVAL '1 second'
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task["task_id"]),
            )
        expired = store.heartbeat_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
            attempt=1,
            lease_epoch=claimed["lease_epoch"],
            worker_id=task["agent_id"],
            claim_id="worker-instance-a",
            traceparent=TRACEPARENT,
        )
        assert expired["accepted"] is False
    finally:
        _cleanup(tenant_id)


def test_expired_lease_reclaim_fences_old_claim_and_progress_rpc_is_idempotent():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        old = _claim(store, tenant_id, run_id, task, "worker-instance-old")
        with store._connect(DSN) as conn:
            conn.execute(
                """
                UPDATE public.devops_tasks
                SET lease_until=clock_timestamp() - INTERVAL '1 second'
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task["task_id"]),
            )
        current = _claim(store, tenant_id, run_id, task, "worker-instance-new")
        assert current["accepted"] is True
        assert current["lease_epoch"] == old["lease_epoch"] + 1

        stale = store.report_task_progress(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
            attempt=1,
            lease_epoch=old["lease_epoch"],
            expected_state_version=old["state_version"],
            percent=40,
            stage="plan",
            message_id="stale-progress",
            traceparent=TRACEPARENT,
            worker_id=task["agent_id"],
            claim_id="worker-instance-old",
        )
        assert stale["accepted"] is False

        args = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "task_id": task["task_id"],
            "attempt": 1,
            "lease_epoch": current["lease_epoch"],
            "expected_state_version": current["state_version"],
            "percent": 50,
            "stage": "plan",
            "message_id": "progress-message-1",
            "traceparent": TRACEPARENT,
            "worker_id": task["agent_id"],
            "claim_id": "worker-instance-new",
        }
        first = store.report_task_progress(**args)
        replay = store.report_task_progress(**args)
        assert first == {"accepted": True, "state_version": current["state_version"] + 1}
        assert replay == first

        conflicting = {**args, "percent": 60}
        with pytest.raises(RpcReplayConflictError):
            store.report_task_progress(**conflicting)
    finally:
        _cleanup(tenant_id)


def test_task_result_is_transactional_and_replay_safe():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        claim = _claim(store, tenant_id, run_id, task)
        args = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "task_id": task["task_id"],
            "attempt": 1,
            "lease_epoch": claim["lease_epoch"],
            "expected_state_version": claim["state_version"],
            "message_id": "result-message-1",
            "traceparent": TRACEPARENT,
            "result": _result(task),
            "worker_id": task["agent_id"],
            "claim_id": "worker-instance-1",
        }
        first = store.report_task_result(**args)
        replay = store.report_task_result(**args)
        assert first == {"accepted": True, "state_version": claim["state_version"] + 1, "status": "completed"}
        assert replay == first

        conflicting = {**args, "result": _result(task, status="failed"), "failure_category": "PERMANENT", "side_effect": "NONE"}
        with pytest.raises(RpcReplayConflictError):
            store.report_task_result(**conflicting)

        with store._connect(DSN) as conn:
            status, result_status = conn.execute(
                """
                SELECT status, result->>'status'
                FROM public.devops_tasks
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task["task_id"]),
            ).fetchone()
            completed_events = conn.execute(
                """
                SELECT count(*) FROM public.devops_event_outbox
                WHERE tenant_id=%s AND task_id=%s AND event_type='task.completed'
                """,
                (tenant_id, task["task_id"]),
            ).fetchone()[0]
        assert status == "completed"
        assert result_status == "passed"
        assert completed_events == 1
    finally:
        _cleanup(tenant_id)


def test_cancel_run_sends_p0_to_active_worker_and_finishes_after_cancel_ack():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        claim = _claim(store, tenant_id, run_id, task)
        cancelled = store.cancel_run(
            tenant_id=tenant_id,
            run_id=run_id,
            idempotency_key="cancel-run-once",
            reason_code="operator_requested",
            requested_by="rusa_supreme_orchestrator",
            traceparent=TRACEPARENT,
        )
        assert cancelled["accepted"] is True
        assert cancelled["status"] == "cancel_requested"
        assert cancelled["cancel_epoch"] == 1

        heartbeat = store.heartbeat_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
            attempt=1,
            lease_epoch=claim["lease_epoch"],
            worker_id=task["agent_id"],
            claim_id="worker-instance-1",
            traceparent=TRACEPARENT,
        )
        assert heartbeat["accepted"] is False

        current_task = store.get_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
            principal=task["agent_id"],
        )
        assert current_task["status"] == "cancel_requested"
        result = _result(task, status="cancelled")
        ack = store.report_task_result(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
            attempt=1,
            lease_epoch=claim["lease_epoch"],
            expected_state_version=current_task["state_version"],
            message_id="cancel-ack-1",
            traceparent=TRACEPARENT,
            result=result,
            side_effect="NONE",
            worker_id=task["agent_id"],
            claim_id="worker-instance-1",
        )
        assert ack["accepted"] is True
        assert ack["status"] == "cancelled"

        replay = store.cancel_run(
            tenant_id=tenant_id,
            run_id=run_id,
            idempotency_key="cancel-run-once",
            reason_code="operator_requested",
            requested_by="rusa_supreme_orchestrator",
            traceparent=TRACEPARENT,
        )
        assert replay["status"] == "cancelled"

        with store._connect(DSN) as conn:
            run_status, cancel_epoch = conn.execute(
                """
                SELECT status, cancel_epoch FROM public.devops_workflows
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()
            cancellation_status = conn.execute(
                """
                SELECT status FROM public.devops_run_cancellations
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
            command_events = conn.execute(
                """
                SELECT envelope FROM public.devops_event_outbox
                WHERE tenant_id=%s AND run_id=%s
                  AND destination='commands' AND event_type='task.cancel.requested'
                """,
                (tenant_id, run_id),
            ).fetchall()
        assert run_status == "cancelled"
        assert cancel_epoch == 1
        assert cancellation_status == "completed"
        assert len(command_events) == 1
        route = command_route(command_events[0][0], allowed_capabilities=("plan",))
        assert route.routing_key == "control.p0"
    finally:
        _cleanup(tenant_id)


def test_cancel_queued_task_is_terminal_without_claiming_a_worker():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        response = store.cancel_run(
            tenant_id=tenant_id,
            run_id=run_id,
            idempotency_key="cancel-queued",
            reason_code="user_requested",
            requested_by="rusa_devops_orchestrator",
            traceparent=TRACEPARENT,
        )
        assert response["status"] == "cancelled"
        current = store.get_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
        )
        assert current["status"] == "cancelled"
        assert current["side_effect"] == "NONE"
        with store._connect(DSN) as conn:
            p0_count = conn.execute(
                """
                SELECT count(*) FROM public.devops_event_outbox
                WHERE tenant_id=%s AND run_id=%s AND event_type='task.cancel.requested'
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
        assert p0_count == 0
    finally:
        _cleanup(tenant_id)


def test_cancel_run_requires_reconciliation_for_prior_applied_side_effects():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        with store._connect(DSN) as conn:
            conn.execute(
                """
                UPDATE public.devops_tasks
                SET status='completed', side_effect='APPLIED'
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task["task_id"]),
            )

        response = store.cancel_run(
            tenant_id=tenant_id,
            run_id=run_id,
            idempotency_key="cancel-after-applied-effect",
            reason_code="operator_requested",
            requested_by="rusa_supreme_orchestrator",
            traceparent=TRACEPARENT,
        )
        assert response["accepted"] is True
        assert response["status"] == "cancel_requested"

        with store._connect(DSN) as conn:
            run_status = conn.execute(
                """
                SELECT status FROM public.devops_workflows
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
            cancellation_status = conn.execute(
                """
                SELECT status FROM public.devops_run_cancellations
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
        assert run_status == "cancel_requested"
        assert cancellation_status == "reconciliation_required"
    finally:
        _cleanup(tenant_id)


def test_cancel_expired_lease_requires_operator_reconciliation():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        _claim(store, tenant_id, run_id, task)
        with store._connect(DSN) as conn:
            conn.execute(
                """
                UPDATE public.devops_tasks
                SET lease_until=clock_timestamp() - INTERVAL '1 second'
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task["task_id"]),
            )
        response = store.cancel_run(
            tenant_id=tenant_id,
            run_id=run_id,
            idempotency_key="cancel-expired-lease",
            reason_code="operator_requested",
            requested_by="rusa_supreme_orchestrator",
            traceparent=TRACEPARENT,
        )
        assert response["status"] == "cancel_requested"
        current = store.get_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
        )
        assert current["status"] == "cancel_requested"
        assert current["side_effect"] == "UNKNOWN"
        with store._connect(DSN) as conn:
            cancellation_status = conn.execute(
                """
                SELECT status FROM public.devops_run_cancellations
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
        assert cancellation_status == "reconciliation_required"
    finally:
        _cleanup(tenant_id)

def test_expired_worker_lease_is_fenced_and_published_as_ambiguous_failure():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        claim = _claim(store, tenant_id, run_id, task)
        with store._connect(DSN) as conn:
            conn.execute(
                """
                UPDATE public.devops_tasks
                SET lease_until=clock_timestamp() - INTERVAL '1 second'
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task["task_id"]),
            )

        swept = store.sweep_expired_leases(tenant_id=tenant_id, limit=10)
        assert swept == {"failed": 1, "reconciliation_required": 0}

        current = store.get_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
        )
        assert current["status"] == "failed"
        assert current["side_effect"] == "UNKNOWN"
        assert current["lease_owner"] is None
        assert current["lease_claim_id"] is None

        with store._connect(DSN) as conn:
            envelope = conn.execute(
                """
                SELECT envelope FROM public.devops_event_outbox
                WHERE tenant_id=%s AND task_id=%s AND event_type='task.failed'
                """,
                (tenant_id, task["task_id"]),
            ).fetchone()[0]
        assert envelope["payload"]["category"] == "AMBIGUOUS"
        assert envelope["payload"]["side_effect"] == "UNKNOWN"
        assert envelope["payload"]["lease_epoch"] == claim["lease_epoch"]

        replay_claim = _claim(store, tenant_id, run_id, task, "worker-instance-replay")
        assert replay_claim["accepted"] is False
    finally:
        _cleanup(tenant_id)


def test_expired_lease_after_cancel_request_requires_reconciliation():
    tenant_id, run_id, _, task = _create_run()
    store = _control_store()
    try:
        _claim(store, tenant_id, run_id, task)
        cancellation = store.cancel_run(
            tenant_id=tenant_id,
            run_id=run_id,
            idempotency_key="cancel-then-expire",
            reason_code="operator_requested",
            requested_by="rusa_supreme_orchestrator",
            traceparent=TRACEPARENT,
        )
        assert cancellation["status"] == "cancel_requested"

        with store._connect(DSN) as conn:
            conn.execute(
                """
                UPDATE public.devops_tasks
                SET lease_until=clock_timestamp() - INTERVAL '1 second'
                WHERE tenant_id=%s AND task_id=%s
                """,
                (tenant_id, task["task_id"]),
            )

        swept = store.sweep_expired_leases(tenant_id=tenant_id)
        assert swept == {"failed": 0, "reconciliation_required": 1}

        current = store.get_task(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task["task_id"],
        )
        assert current["status"] == "cancel_requested"
        assert current["side_effect"] == "UNKNOWN"
        assert current["lease_owner"] is None

        with store._connect(DSN) as conn:
            cancellation_status = conn.execute(
                """
                SELECT status FROM public.devops_run_cancellations
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
            envelope = conn.execute(
                """
                SELECT envelope FROM public.devops_event_outbox
                WHERE tenant_id=%s AND task_id=%s AND event_type='audit.recorded'
                  AND envelope->'payload'->>'action'='task.cancel.lease_expired'
                """,
                (tenant_id, task["task_id"]),
            ).fetchone()[0]
        assert cancellation_status == "reconciliation_required"
        assert envelope["payload"]["outcome"] == "reconciliation_required"
    finally:
        _cleanup(tenant_id)

