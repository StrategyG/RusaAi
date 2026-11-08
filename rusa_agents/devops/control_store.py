"""Tenant-bound PostgreSQL operations for the internal DevOps control RPCs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .agent_registry import AgentAuthorizationError, authorize_agent_task
from .contracts import (
    ContractValidationError,
    validate_agent_result_for_task,
    validate_workflow_event,
)
from .store import WorkflowIdempotencyConflictError

CONTROL_SERVICE_ID = "rusa_devops_control_service"
_TRACEPARENT_RE = re.compile(
    r"^00-(?!0{32}-)[a-f0-9]{32}-(?!0{16}-)[a-f0-9]{16}-[a-f0-9]{2}$"
)
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_DOMAIN_EVENTS = {
    "plan": {"plan.completed"},
    "code": {"code.completed"},
    "build": {"build.completed"},
    "test": {"test.completed"},
    "release": {"release.approved"},
    "deploy": {"deploy.completed"},
    "operate": {"operate.incident_detected", "operate.recovered"},
    "monitor": {"monitor.alert_triggered", "monitor.feedback_created"},
}
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "blocked", "cancelled", "skipped"}
)
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "blocked", "cancelled"})


class WorkflowNotFoundError(LookupError):
    """The requested workflow or task is not visible in the authenticated tenant."""


class TaskAccessDeniedError(PermissionError):
    """The authenticated workload is not assigned to the requested task."""


class TaskPolicyDeniedError(PermissionError):
    """The task's requested capabilities exceed the authenticated agent policy."""


class TaskStateConflictError(RuntimeError):
    """A task mutation lost its state-version or lease compare-and-set."""


class RpcReplayConflictError(ValueError):
    """An RPC message ID was reused with a different request body."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_traceparent(value: str) -> None:
    if not isinstance(value, str) or not _TRACEPARENT_RE.fullmatch(value):
        raise ValueError("traceparent must be a valid W3C version 00 traceparent")


def _validate_reason_code(value: str) -> None:
    if not isinstance(value, str) or not _REASON_RE.fullmatch(value):
        raise ValueError("reason_code must be a lowercase stable code")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class PostgresWorkflowControlStore:
    """CAS/lease-fenced store used by the internal gRPC WorkflowControl service.

    API operations use the API runtime login; task execution and report RPCs use
    the worker runtime login. The relay login is deliberately never accepted.
    """

    def __init__(
        self,
        *,
        api_dsn: str | None = None,
        worker_dsn: str | None = None,
        lease_seconds: int = 30,
        statement_timeout_ms: int = 2000,
    ):
        if not (
            isinstance(api_dsn, str) and api_dsn.strip()
            or isinstance(worker_dsn, str) and worker_dsn.strip()
        ):
            raise ValueError("at least one scoped PostgreSQL runtime DSN is required")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if (
            isinstance(statement_timeout_ms, bool)
            or not 100 <= statement_timeout_ms <= 30_000
        ):
            raise ValueError("statement_timeout_ms must be between 100 and 30000")
        self.api_dsn = api_dsn
        self.worker_dsn = worker_dsn
        self.lease_seconds = lease_seconds
        self.statement_timeout_ms = statement_timeout_ms

    @staticmethod
    def _connect(dsn: str | None):
        if not dsn:
            raise PermissionError("this gRPC service profile does not hold the required database role")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use PostgresWorkflowControlStore") from exc
        return psycopg.connect(dsn)

    def _prepare(self, conn: Any, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        conn.execute(
            "SELECT set_config('rusa.tenant_id', %s, true)",
            (tenant_id,),
        )
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{self.statement_timeout_ms}ms",),
        )
        conn.execute(
            "SELECT set_config('lock_timeout', %s, true)",
            (f"{min(self.statement_timeout_ms, 750)}ms",),
        )

    @staticmethod
    def _workflow_row(conn: Any, tenant_id: str, run_id: str, *, lock: bool = False):
        sql = """
            SELECT run_id, tenant_id, status, state_version, result_ref,
                   created_at, updated_at, cancel_epoch
            FROM public.devops_workflows
            WHERE tenant_id=%s AND run_id=%s
        """
        if lock:
            row = conn.execute(
                """
                SELECT run_id, tenant_id, status, state_version, result_ref,
                       created_at, updated_at, cancel_epoch
                FROM public.devops_workflows
                WHERE tenant_id=%s AND run_id=%s
                FOR UPDATE
                """,
                (tenant_id, run_id),
            ).fetchone()
        else:
            row = conn.execute(sql, (tenant_id, run_id)).fetchone()
        if row is None:
            raise WorkflowNotFoundError("workflow not found")
        return {
            "run_id": row[0],
            "tenant_id": row[1],
            "status": row[2],
            "state_version": row[3],
            "result_ref": row[4] or "",
            "created_at": row[5],
            "updated_at": row[6],
            "cancel_epoch": row[7],
        }

    @staticmethod
    def _task_row(
        conn: Any,
        tenant_id: str,
        run_id: str,
        task_id: str,
        *,
        lock: bool = False,
    ):
        params = (tenant_id, run_id, task_id)
        if lock:
            row = conn.execute(
                """
                SELECT t.task_id, t.run_id, t.tenant_id, t.agent_id, t.stage,
                       t.attempt, t.status, t.state_version, t.lease_epoch,
                       t.deadline_at, t.task, t.result, t.lease_owner, t.lease_claim_id,
                       t.lease_until, t.heartbeat_at, t.side_effect, w.status, w.cancel_epoch
                FROM public.devops_tasks AS t
                JOIN public.devops_workflows AS w
                  ON w.tenant_id=t.tenant_id AND w.run_id=t.run_id
                WHERE t.tenant_id=%s AND t.run_id=%s AND t.task_id=%s
                FOR UPDATE
                """,
                params,
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT t.task_id, t.run_id, t.tenant_id, t.agent_id, t.stage,
                       t.attempt, t.status, t.state_version, t.lease_epoch,
                       t.deadline_at, t.task, t.result, t.lease_owner, t.lease_claim_id,
                       t.lease_until, t.heartbeat_at, t.side_effect, w.status, w.cancel_epoch
                FROM public.devops_tasks AS t
                JOIN public.devops_workflows AS w
                  ON w.tenant_id=t.tenant_id AND w.run_id=t.run_id
                WHERE t.tenant_id=%s AND t.run_id=%s AND t.task_id=%s
                """,
                params,
            ).fetchone()
        if row is None:
            raise WorkflowNotFoundError("task not found")
        return {
            "task_id": row[0],
            "run_id": row[1],
            "tenant_id": row[2],
            "agent_id": row[3],
            "stage": row[4],
            "attempt": row[5],
            "status": row[6],
            "state_version": row[7],
            "lease_epoch": row[8],
            "deadline_at": row[9],
            "task": row[10],
            "result": row[11],
            "lease_owner": row[12],
            "lease_claim_id": row[13],
            "lease_until": row[14],
            "heartbeat_at": row[15],
            "side_effect": row[16],
            "run_status": row[17],
            "cancel_epoch": row[18],
        }

    @staticmethod
    def _event_context(conn: Any, tenant_id: str, run_id: str, task_id: str) -> tuple[str, str]:
        row = conn.execute(
            """
            SELECT envelope->>'correlation_id', envelope->>'traceparent'
            FROM public.devops_event_outbox
            WHERE tenant_id=%s AND run_id=%s AND task_id=%s
              AND event_type='task.request'
            ORDER BY outbox_id
            LIMIT 1
            """,
            (tenant_id, run_id, task_id),
        ).fetchone()
        if row is None:
            return run_id, "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        return row[0] or run_id, row[1] or (
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        )

    @staticmethod
    def _append_event(
        conn: Any,
        *,
        tenant_id: str,
        event: dict[str, Any],
        destination: str,
        aggregate_version: int,
    ) -> None:
        validate_workflow_event(event)
        if event["tenant_id"] != tenant_id:
            raise ValueError("event tenant_id does not match authenticated tenant")
        if destination not in {"commands", "events"}:
            raise ValueError("invalid outbox destination")
        conn.execute(
            """
            INSERT INTO public.devops_event_outbox
                (tenant_id, run_id, task_id, attempt, message_id,
                 destination, event_type, aggregate_version, envelope)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                tenant_id,
                event["run_id"],
                event.get("task_id"),
                event.get("attempt"),
                event["message_id"],
                destination,
                event["type"],
                aggregate_version,
                _canonical_json(event),
            ),
        )

    def _event(
        self,
        *,
        tenant_id: str,
        run_id: str,
        task_id: str | None,
        attempt: int | None,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
        traceparent: str,
    ) -> dict[str, Any]:
        _validate_traceparent(traceparent)
        event: dict[str, Any] = {
            "schema_version": 1,
            "type": event_type,
            "message_id": str(uuid4()),
            "tenant_id": tenant_id,
            "run_id": run_id,
            "correlation_id": correlation_id,
            "traceparent": traceparent,
            "producer": CONTROL_SERVICE_ID,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        if task_id is not None:
            event["task_id"] = task_id
        if attempt is not None:
            event["attempt"] = attempt
        return event

    def get_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        principal: str | None = None,
    ) -> dict[str, Any]:
        dsn = self.worker_dsn if principal else self.api_dsn
        with self._connect(dsn) as conn:
            self._prepare(conn, tenant_id)
            workflow = self._workflow_row(conn, tenant_id, run_id)
            if principal:
                assigned = conn.execute(
                    """
                    SELECT 1 FROM public.devops_tasks
                    WHERE tenant_id=%s AND run_id=%s AND agent_id=%s
                    LIMIT 1
                    """,
                    (tenant_id, run_id, principal),
                ).fetchone()
                if assigned is None:
                    raise WorkflowNotFoundError("workflow not found")
            return workflow

    def get_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        dsn = self.worker_dsn if principal else self.api_dsn
        with self._connect(dsn) as conn:
            self._prepare(conn, tenant_id)
            if run_id is None:
                row = conn.execute(
                    """
                    SELECT run_id FROM public.devops_tasks
                    WHERE tenant_id=%s AND task_id=%s
                    """,
                    (tenant_id, task_id),
                ).fetchone()
                if row is None:
                    raise WorkflowNotFoundError("task not found")
                run_id = row[0]
            task = self._task_row(conn, tenant_id, run_id, task_id)
            if principal and task["agent_id"] != principal:
                raise TaskAccessDeniedError("workload is not assigned to this task")
            return task

    def claim_task(
        self,
        *,
        tenant_id: str,
        run_id: str,
        task_id: str,
        attempt: int,
        worker_id: str,
        claim_id: str,
        traceparent: str,
    ) -> dict[str, Any]:
        _validate_traceparent(traceparent)
        if not isinstance(claim_id, str) or not 1 <= len(claim_id) <= 128:
            raise ValueError("claim_id must contain 1..128 characters")
        with self._connect(self.worker_dsn) as conn:
            self._prepare(conn, tenant_id)
            workflow = self._workflow_row(conn, tenant_id, run_id, lock=True)
            task = self._task_row(conn, tenant_id, run_id, task_id, lock=True)
            if task["agent_id"] != worker_id:
                raise TaskAccessDeniedError("workload is not assigned to this task")
            try:
                authorize_agent_task(
                    task["task"],
                    authenticated_principal=worker_id,
                )
            except (AgentAuthorizationError, ContractValidationError) as exc:
                raise TaskPolicyDeniedError(
                    "task capability policy denied this workload"
                ) from exc
            if task["attempt"] != attempt:
                return self._claim_response(task, workflow, accepted=False)
            if workflow["status"] != "running" and workflow["status"] != "queued":
                return self._claim_response(task, workflow, accepted=False)
            now = datetime.now(timezone.utc)
            deadline = _as_utc(task["deadline_at"])
            if deadline is not None and deadline <= now:
                return self._claim_response(task, workflow, accepted=False, status="expired")

            lease_until = _as_utc(task["lease_until"])
            if (
                task["status"] in {"accepted", "running"}
                and task["lease_owner"] == worker_id
                and task["lease_claim_id"] == claim_id
                and lease_until is not None
                and lease_until > now
            ):
                return self._claim_response(task, workflow, accepted=True)

            reclaimable = task["status"] == "queued" or (
                task["status"] in {"accepted", "running"}
                and (lease_until is None or lease_until <= now)
            )
            if not reclaimable:
                return self._claim_response(task, workflow, accepted=False)

            updated = conn.execute(
                """
                UPDATE public.devops_tasks
                SET status='accepted',
                    lease_epoch=lease_epoch + 1,
                    lease_owner=%s,
                    lease_claim_id=%s,
                    lease_until=clock_timestamp() + (%s * INTERVAL '1 second'),
                    heartbeat_at=clock_timestamp(),
                    state_version=state_version + 1,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                  AND (
                      status='queued'
                      OR (
                          status IN ('accepted','running')
                          AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                      )
                  )
                  AND (deadline_at IS NULL OR deadline_at > clock_timestamp())
                RETURNING state_version, lease_epoch, lease_until, deadline_at, status
                """,
                (
                    worker_id,
                    claim_id,
                    self.lease_seconds,
                    tenant_id,
                    run_id,
                    task_id,
                ),
            ).fetchone()
            if updated is None:
                return self._claim_response(task, workflow, accepted=False)

            if workflow["status"] == "queued":
                run_version = workflow["state_version"] + 1
                conn.execute(
                    """
                    UPDATE public.devops_workflows
                    SET status='running', state_version=%s, updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND run_id=%s AND state_version=%s
                    """,
                    (run_version, tenant_id, run_id, workflow["state_version"]),
                )
                workflow["status"] = "running"
                workflow["state_version"] = run_version
                run_correlation, _ = self._event_context(conn, tenant_id, run_id, task_id)
                started = self._event(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_id=None,
                    attempt=None,
                    event_type="audit.recorded",
                    payload={
                        "action": "workflow.started",
                        "outcome": "running",
                        "resource_ref": f"workflow://{run_id}",
                        "state_version": run_version,
                    },
                    correlation_id=run_correlation,
                    traceparent=traceparent,
                )
                self._append_event(
                    conn,
                    tenant_id=tenant_id,
                    event=started,
                    destination="events",
                    aggregate_version=run_version,
                )
            task["state_version"] = updated[0]
            task["lease_epoch"] = updated[1]
            task["lease_until"] = updated[2]
            task["deadline_at"] = updated[3]
            task["status"] = updated[4]
            correlation_id, default_trace = self._event_context(
                conn, tenant_id, run_id, task_id
            )
            event = self._event(
                tenant_id=tenant_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                event_type="task.accepted",
                payload={
                    "worker_id": worker_id,
                    "lease_epoch": task["lease_epoch"],
                    "state_version": task["state_version"],
                },
                correlation_id=correlation_id,
                traceparent=traceparent or default_trace,
            )
            self._append_event(
                conn,
                tenant_id=tenant_id,
                event=event,
                destination="events",
                aggregate_version=task["state_version"],
            )
            return self._claim_response(task, workflow, accepted=True)

    @staticmethod
    def _claim_response(
        task: dict[str, Any],
        workflow: dict[str, Any],
        *,
        accepted: bool,
        status: str | None = None,
    ) -> dict[str, Any]:
        return {
            "accepted": accepted,
            "lease_epoch": task["lease_epoch"],
            "state_version": task["state_version"],
            "lease_owner": task["lease_owner"] or "",
            "lease_until": task["lease_until"],
            "deadline_at": task["deadline_at"],
            "status": status or task["status"],
            "cancel_epoch": workflow["cancel_epoch"],
        }

    def heartbeat_task(
        self,
        *,
        tenant_id: str,
        run_id: str,
        task_id: str,
        attempt: int,
        lease_epoch: int,
        worker_id: str,
        claim_id: str,
        traceparent: str,
    ) -> dict[str, Any]:
        _validate_traceparent(traceparent)
        if not isinstance(claim_id, str) or not 1 <= len(claim_id) <= 128:
            raise ValueError("claim_id must contain 1..128 characters")
        with self._connect(self.worker_dsn) as conn:
            self._prepare(conn, tenant_id)
            workflow = self._workflow_row(conn, tenant_id, run_id, lock=True)
            task = self._task_row(conn, tenant_id, run_id, task_id, lock=True)
            now = datetime.now(timezone.utc)
            deadline = _as_utc(task["deadline_at"])
            lease_until = _as_utc(task["lease_until"])
            accepted = (
                task["agent_id"] == worker_id
                and task["attempt"] == attempt
                and task["lease_epoch"] == lease_epoch
                and task["lease_owner"] == worker_id
                and task["lease_claim_id"] == claim_id
                and task["status"] in {"accepted", "running"}
                and workflow["status"] in {"queued", "running"}
                and lease_until is not None
                and lease_until > now
                and (deadline is None or deadline > now)
            )
            if not accepted:
                if task["agent_id"] != worker_id:
                    raise TaskAccessDeniedError("workload is not assigned to this task")
                return {
                    "accepted": False,
                    "state_version": task["state_version"],
                    "lease_until": task["lease_until"],
                    "cancel_epoch": workflow["cancel_epoch"],
                    "run_status": workflow["status"],
                    "task_status": task["status"],
                }

            updated = conn.execute(
                """
                UPDATE public.devops_tasks
                SET status='running',
                    lease_until=clock_timestamp() + (%s * INTERVAL '1 second'),
                    heartbeat_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                  AND attempt=%s AND lease_epoch=%s AND lease_owner=%s
                  AND lease_claim_id=%s
                  AND lease_until > clock_timestamp()
                  AND (deadline_at IS NULL OR deadline_at > clock_timestamp())
                RETURNING lease_until
                """,
                (
                    self.lease_seconds,
                    tenant_id,
                    run_id,
                    task_id,
                    attempt,
                    lease_epoch,
                    worker_id,
                    claim_id,
                ),
            ).fetchone()
            if updated is None:
                return {
                    "accepted": False,
                    "state_version": task["state_version"],
                    "lease_until": task["lease_until"],
                    "cancel_epoch": workflow["cancel_epoch"],
                    "run_status": workflow["status"],
                    "task_status": task["status"],
                }
            return {
                "accepted": True,
                "state_version": task["state_version"],
                "lease_until": updated[0],
                "cancel_epoch": workflow["cancel_epoch"],
                "run_status": workflow["status"],
                "task_status": "running",
            }

    def sweep_expired_leases(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
    ) -> dict[str, int]:
        """Fence expired worker leases and publish recovery facts atomically.

        Lock workflow before task, matching every other control-plane mutation.
        An ordinary expired lease becomes an ambiguous task failure; the
        orchestrator must reconcile its possible side effects before retrying.
        A task whose run is already cancelling remains cancel_requested and
        moves the cancellation record to reconciliation_required.
        """
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        swept = {"failed": 0, "reconciliation_required": 0}
        with self._connect(self.worker_dsn) as conn:
            self._prepare(conn, tenant_id)
            candidates = conn.execute(
                """
                SELECT run_id, task_id
                FROM public.devops_tasks
                WHERE tenant_id=%s
                  AND status IN ('accepted','running','validating','cancel_requested')
                  AND lease_until IS NOT NULL
                  AND lease_until <= clock_timestamp()
                ORDER BY lease_until, task_id
                LIMIT %s
                """,
                (tenant_id, limit),
            ).fetchall()

            for run_id, task_id in candidates:
                # All control-plane writers lock the workflow before its task.
                workflow = self._workflow_row(conn, tenant_id, run_id, lock=True)
                expired = conn.execute(
                    """
                    SELECT attempt, lease_epoch, state_version, status
                    FROM public.devops_tasks
                    WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                      AND status IN ('accepted','running','validating','cancel_requested')
                      AND lease_until IS NOT NULL
                      AND lease_until <= clock_timestamp()
                    FOR UPDATE
                    """,
                    (tenant_id, run_id, task_id),
                ).fetchone()
                if expired is None:
                    continue

                attempt, lease_epoch, old_version, task_status = expired
                new_version = old_version + 1
                is_cancelling = (
                    task_status == "cancel_requested"
                    or workflow["status"] == "cancel_requested"
                )
                conn.execute(
                    """
                    UPDATE public.devops_tasks
                    SET status=%s,
                        state_version=%s,
                        side_effect='UNKNOWN',
                        lease_owner=NULL,
                        lease_claim_id=NULL,
                        lease_until=NULL,
                        updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                    """,
                    (
                        "cancel_requested" if is_cancelling else "failed",
                        new_version,
                        tenant_id,
                        run_id,
                        task_id,
                    ),
                )
                correlation_id, traceparent = self._event_context(
                    conn, tenant_id, run_id, task_id
                )
                if is_cancelling:
                    conn.execute(
                        """
                        UPDATE public.devops_run_cancellations
                        SET status='reconciliation_required'
                        WHERE tenant_id=%s AND run_id=%s AND status='requested'
                        """,
                        (tenant_id, run_id),
                    )
                    event = self._event(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        event_type="audit.recorded",
                        payload={
                            "action": "task.cancel.lease_expired",
                            "outcome": "reconciliation_required",
                            "resource_ref": f"task://{task_id}/{attempt}",
                            "state_version": new_version,
                        },
                        correlation_id=correlation_id,
                        traceparent=traceparent,
                    )
                    swept["reconciliation_required"] += 1
                else:
                    event = self._event(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        event_type="task.failed",
                        payload={
                            "lease_epoch": lease_epoch,
                            "state_version": new_version,
                            "code": "worker_lease_expired",
                            "category": "AMBIGUOUS",
                            "side_effect": "UNKNOWN",
                        },
                        correlation_id=correlation_id,
                        traceparent=traceparent,
                    )
                    swept["failed"] += 1
                self._append_event(
                    conn,
                    tenant_id=tenant_id,
                    event=event,
                    destination="events",
                    aggregate_version=new_version,
                )
        return swept

    def _rpc_receipt(
        self,
        conn: Any,
        *,
        tenant_id: str,
        task_id: str,
        method: str,
        message_id: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT request_sha256, response
            FROM public.devops_task_rpc_receipts
            WHERE tenant_id=%s AND task_id=%s AND method=%s AND message_id=%s
            """,
            (tenant_id, task_id, method, message_id),
        ).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise RpcReplayConflictError(
                "RPC message_id was replayed with a different request body"
            )
        return row[1]

    @staticmethod
    def _save_rpc_receipt(
        conn: Any,
        *,
        tenant_id: str,
        task_id: str,
        method: str,
        message_id: str,
        request_hash: str,
        response: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO public.devops_task_rpc_receipts
                (tenant_id, task_id, method, message_id, request_sha256, response)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                tenant_id,
                task_id,
                method,
                message_id,
                request_hash,
                _canonical_json(response),
            ),
        )

    def report_task_progress(
        self,
        *,
        tenant_id: str,
        run_id: str,
        task_id: str,
        attempt: int,
        lease_epoch: int,
        expected_state_version: int,
        percent: int,
        stage: str,
        message_id: str,
        traceparent: str,
        worker_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        _validate_traceparent(traceparent)
        if not isinstance(claim_id, str) or not 1 <= len(claim_id) <= 128:
            raise ValueError("claim_id must contain 1..128 characters")
        if not message_id or len(message_id) > 128:
            raise ValueError("message_id must contain 1..128 characters")
        if isinstance(percent, bool) or not 0 <= percent <= 100:
            raise ValueError("percent must be between 0 and 100")
        request_body = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "lease_epoch": lease_epoch,
            "expected_state_version": expected_state_version,
            "percent": percent,
            "stage": stage,
            "traceparent": traceparent,
            "claim_id": claim_id,
        }
        request_hash = _sha256(request_body)
        with self._connect(self.worker_dsn) as conn:
            self._prepare(conn, tenant_id)
            replay = self._rpc_receipt(
                conn,
                tenant_id=tenant_id,
                task_id=task_id,
                method="ReportTaskProgress",
                message_id=message_id,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            workflow = self._workflow_row(conn, tenant_id, run_id, lock=True)
            task = self._task_row(conn, tenant_id, run_id, task_id, lock=True)
            if task["agent_id"] != worker_id:
                raise TaskAccessDeniedError("workload is not assigned to this task")
            now = datetime.now(timezone.utc)
            lease_until = _as_utc(task["lease_until"])
            deadline = _as_utc(task["deadline_at"])
            accepted = (
                task["attempt"] == attempt
                and task["lease_epoch"] == lease_epoch
                and task["lease_owner"] == worker_id
                and task["lease_claim_id"] == claim_id
                and task["state_version"] == expected_state_version
                and task["status"] in {"accepted", "running"}
                and workflow["status"] in {"queued", "running"}
                and lease_until is not None
                and lease_until > now
                and (deadline is None or deadline > now)
                and stage == task["stage"]
            )
            if not accepted:
                response = {
                    "accepted": False,
                    "state_version": task["state_version"],
                }
                self._save_rpc_receipt(
                    conn,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    method="ReportTaskProgress",
                    message_id=message_id,
                    request_hash=request_hash,
                    response=response,
                )
                return response

            new_version = task["state_version"] + 1
            updated = conn.execute(
                """
                UPDATE public.devops_tasks
                SET status='running', state_version=%s, updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                  AND attempt=%s AND lease_epoch=%s AND state_version=%s
                  AND lease_owner=%s AND lease_claim_id=%s
                  AND lease_until > clock_timestamp()
                  AND (deadline_at IS NULL OR deadline_at > clock_timestamp())
                RETURNING state_version
                """,
                (
                    new_version,
                    tenant_id,
                    run_id,
                    task_id,
                    attempt,
                    lease_epoch,
                    expected_state_version,
                    worker_id,
                    claim_id,
                ),
            ).fetchone()
            if updated is None:
                response = {
                    "accepted": False,
                    "state_version": task["state_version"],
                }
                self._save_rpc_receipt(
                    conn,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    method="ReportTaskProgress",
                    message_id=message_id,
                    request_hash=request_hash,
                    response=response,
                )
                return response
            correlation_id, _ = self._event_context(conn, tenant_id, run_id, task_id)
            event = self._event(
                tenant_id=tenant_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                event_type="task.progress",
                payload={
                    "lease_epoch": lease_epoch,
                    "state_version": new_version,
                    "percent": percent,
                    "stage": stage,
                },
                correlation_id=correlation_id,
                traceparent=traceparent,
            )
            self._append_event(
                conn,
                tenant_id=tenant_id,
                event=event,
                destination="events",
                aggregate_version=new_version,
            )
            response = {"accepted": True, "state_version": new_version}
            self._save_rpc_receipt(
                conn,
                tenant_id=tenant_id,
                task_id=task_id,
                method="ReportTaskProgress",
                message_id=message_id,
                request_hash=request_hash,
                response=response,
            )
            return response

    def report_task_result(
        self,
        *,
        tenant_id: str,
        run_id: str,
        task_id: str,
        attempt: int,
        lease_epoch: int,
        expected_state_version: int,
        message_id: str,
        traceparent: str,
        result: dict[str, Any],
        failure_category: str = "",
        side_effect: str = "",
        diagnostic_ref: str = "",
        worker_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        _validate_traceparent(traceparent)
        if not isinstance(claim_id, str) or not 1 <= len(claim_id) <= 128:
            raise ValueError("claim_id must contain 1..128 characters")
        if not message_id or len(message_id) > 128:
            raise ValueError("message_id must contain 1..128 characters")
        request_body = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "lease_epoch": lease_epoch,
            "expected_state_version": expected_state_version,
            "traceparent": traceparent,
            "result": result,
            "failure_category": failure_category,
            "side_effect": side_effect,
            "diagnostic_ref": diagnostic_ref,
            "claim_id": claim_id,
        }
        request_hash = _sha256(request_body)
        with self._connect(self.worker_dsn) as conn:
            self._prepare(conn, tenant_id)
            replay = self._rpc_receipt(
                conn,
                tenant_id=tenant_id,
                task_id=task_id,
                method="ReportTaskResult",
                message_id=message_id,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            workflow = self._workflow_row(conn, tenant_id, run_id, lock=True)
            task = self._task_row(conn, tenant_id, run_id, task_id, lock=True)
            if task["agent_id"] != worker_id:
                raise TaskAccessDeniedError("workload is not assigned to this task")
            is_cancel_ack = result.get("status") == "cancelled"
            task_status_allowed = task["status"] in {"accepted", "running"} or (
                is_cancel_ack and task["status"] == "cancel_requested"
            )
            run_status_allowed = workflow["status"] in {"queued", "running"} or (
                is_cancel_ack and workflow["status"] == "cancel_requested"
            )
            if (
                task["attempt"] != attempt
                or task["lease_epoch"] != lease_epoch
                or task["lease_owner"] != worker_id
                or task["lease_claim_id"] != claim_id
                or task["state_version"] != expected_state_version
                or not task_status_allowed
                or not run_status_allowed
                or _as_utc(task["lease_until"]) is None
                or _as_utc(task["lease_until"]) <= datetime.now(timezone.utc)
            ):
                response = {
                    "accepted": False,
                    "state_version": task["state_version"],
                    "status": task["status"],
                }
                self._save_rpc_receipt(
                    conn,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    method="ReportTaskResult",
                    message_id=message_id,
                    request_hash=request_hash,
                    response=response,
                )
                return response

            validate_agent_result_for_task(task["task"], result)
            status = result["status"]
            stage = task["stage"]
            if status == "passed" and stage == "release":
                approval_ref = result.get("output", {}).get("approval_ref")
                if not approval_ref:
                    raise ContractValidationError(
                        "a passed Release Agent result requires an explicit approval_ref"
                    )
            if status == "passed" and stage == "deploy":
                environment = task["task"].get("input", {}).get("environment")
                constraints = task["task"].get("constraints", {})
                if environment == "production" and not constraints.get("approval_ref"):
                    raise ContractValidationError(
                        "production deployment requires a pre-authorized approval_ref"
                    )
            if status == "failed":
                if failure_category not in {"TRANSIENT", "PERMANENT", "POLICY", "AMBIGUOUS"}:
                    raise ValueError("failed result requires a valid failure_category")
                if side_effect not in {"NONE", "APPLIED", "UNKNOWN"}:
                    raise ValueError("failed result requires a valid side_effect")
            if status == "cancelled" and side_effect not in {"NONE", "APPLIED", "UNKNOWN"}:
                raise ValueError("cancelled result requires a valid side_effect")

            task_status = {
                "passed": "completed",
                "failed": "failed",
                "blocked": "blocked",
                "needs_approval": "needs_approval",
                "cancelled": "cancelled",
                "skipped": "skipped",
            }[status]
            new_version = task["state_version"] + 1
            result_json = _canonical_json(result)
            updated = conn.execute(
                """
                UPDATE public.devops_tasks
                SET status=%s, state_version=%s, result=%s::jsonb,
                    side_effect=%s, lease_owner=NULL, lease_claim_id=NULL,
                    lease_until=NULL,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                  AND attempt=%s AND lease_epoch=%s AND state_version=%s
                  AND lease_owner=%s AND lease_claim_id=%s
                  AND lease_until > clock_timestamp()
                  AND (
                      %s = 'cancelled'
                      OR deadline_at IS NULL
                      OR deadline_at > clock_timestamp()
                  )
                RETURNING state_version
                """,
                (
                    task_status,
                    new_version,
                    result_json,
                    side_effect or None,
                    tenant_id,
                    run_id,
                    task_id,
                    attempt,
                    lease_epoch,
                    expected_state_version,
                    worker_id,
                    claim_id,
                    status,
                ),
            ).fetchone()
            if updated is None:
                response = {
                    "accepted": False,
                    "state_version": task["state_version"],
                    "status": task["status"],
                }
                self._save_rpc_receipt(
                    conn,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    method="ReportTaskResult",
                    message_id=message_id,
                    request_hash=request_hash,
                    response=response,
                )
                return response
            correlation_id, _ = self._event_context(conn, tenant_id, run_id, task_id)
            event: dict[str, Any] | None = None
            destination = "events"
            aggregate_version = new_version
            if status == "passed":
                handoff_event = result.get("handoff", {}).get("event")
                allowed = _DOMAIN_EVENTS.get(stage, set())
                if handoff_event not in allowed:
                    if len(allowed) == 1:
                        handoff_event = next(iter(allowed))
                    else:
                        raise ContractValidationError(
                            f"passed {stage} result requires a valid stage domain event"
                        )
                result_ref = f"rusa://devops/results/{task_id}/{attempt}"
                event = self._event(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    event_type="task.completed",
                    payload={
                        "lease_epoch": lease_epoch,
                        "state_version": new_version,
                        "result_ref": result_ref,
                        "sha256": _sha256(result),
                        "stage": stage,
                        "domain_event": handoff_event,
                    },
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                )
            elif status == "failed":
                findings = result.get("output", {}).get("findings", [])
                code = findings[0]["code"] if findings else "agent_failed"
                payload = {
                    "lease_epoch": lease_epoch,
                    "state_version": new_version,
                    "code": code,
                    "category": failure_category,
                    "side_effect": side_effect,
                }
                if diagnostic_ref:
                    payload["diagnostic_ref"] = diagnostic_ref
                event = self._event(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    event_type="task.failed",
                    payload=payload,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                )
            elif status in {"blocked", "needs_approval"}:
                workflow_status = "blocked" if status == "blocked" else "needs_approval"
                run_version = workflow["state_version"] + 1
                conn.execute(
                    """
                    UPDATE public.devops_workflows
                    SET status=%s, state_version=%s, updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND run_id=%s AND state_version=%s
                    """,
                    (
                        workflow_status,
                        run_version,
                        tenant_id,
                        run_id,
                        workflow["state_version"],
                    ),
                )
                findings = result.get("output", {}).get("findings", [])
                reason = (
                    "approval_required"
                    if status == "needs_approval"
                    else (findings[0]["code"] if findings else "task_blocked")
                )
                event = self._event(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_id=None,
                    attempt=None,
                    event_type="workflow.blocked",
                    payload={
                        "state_version": run_version,
                        "stage": stage,
                        "reason_code": reason,
                    },
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                )
                aggregate_version = run_version
            elif status == "cancelled":
                if workflow["cancel_epoch"] < 1:
                    raise ContractValidationError(
                        "task cannot report cancelled without an accepted workflow cancellation"
                    )
                event = self._event(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    event_type="task.cancelled",
                    payload={
                        "cancel_epoch": workflow["cancel_epoch"],
                        "state_version": new_version,
                        "side_effect": side_effect,
                    },
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                )
                if side_effect != "NONE":
                    conn.execute(
                        """
                        UPDATE public.devops_run_cancellations
                        SET status='reconciliation_required'
                        WHERE tenant_id=%s AND run_id=%s AND status='requested'
                        """,
                        (tenant_id, run_id),
                    )
                else:
                    remaining = conn.execute(
                        """
                        SELECT count(*)
                        FROM public.devops_tasks
                        WHERE tenant_id=%s AND run_id=%s
                          AND status NOT IN ('completed','failed','blocked','cancelled','skipped')
                        """,
                        (tenant_id, run_id),
                    ).fetchone()[0]
                    unresolved_effects = conn.execute(
                        """
                        SELECT count(*)
                        FROM public.devops_tasks
                        WHERE tenant_id=%s AND run_id=%s
                          AND COALESCE(side_effect, 'NONE') <> 'NONE'
                        """,
                        (tenant_id, run_id),
                    ).fetchone()[0]
                    if remaining == 0 and unresolved_effects == 0:
                        run_version = workflow["state_version"] + 1
                        conn.execute(
                            """
                            UPDATE public.devops_workflows
                            SET status='cancelled', state_version=%s,
                                updated_at=clock_timestamp()
                            WHERE tenant_id=%s AND run_id=%s
                            """,
                            (run_version, tenant_id, run_id),
                        )
                        conn.execute(
                            """
                            UPDATE public.devops_run_cancellations
                            SET status='completed', completed_at=clock_timestamp()
                            WHERE tenant_id=%s AND run_id=%s
                              AND status IN ('requested','reconciliation_required')
                            """,
                            (tenant_id, run_id),
                        )
                        completion_event = self._event(
                            tenant_id=tenant_id,
                            run_id=run_id,
                            task_id=None,
                            attempt=None,
                            event_type="audit.recorded",
                            payload={
                                "action": "workflow.cancelled",
                                "outcome": "cancelled",
                                "resource_ref": f"workflow://{run_id}",
                                "state_version": run_version,
                            },
                            correlation_id=correlation_id,
                            traceparent=traceparent,
                        )
                        self._append_event(
                            conn,
                            tenant_id=tenant_id,
                            event=completion_event,
                            destination="events",
                            aggregate_version=run_version,
                        )
            else:
                event = self._event(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    event_type="audit.recorded",
                    payload={
                        "action": "task.skipped",
                        "outcome": "skipped",
                        "resource_ref": f"task://{task_id}/{attempt}",
                        "state_version": new_version,
                    },
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                )

            if event is not None:
                self._append_event(
                    conn,
                    tenant_id=tenant_id,
                    event=event,
                    destination=destination,
                    aggregate_version=aggregate_version,
                )
            response = {
                "accepted": True,
                "state_version": new_version,
                "status": task_status,
            }
            self._save_rpc_receipt(
                conn,
                tenant_id=tenant_id,
                task_id=task_id,
                method="ReportTaskResult",
                message_id=message_id,
                request_hash=request_hash,
                response=response,
            )
            return response

    def cancel_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        idempotency_key: str,
        reason_code: str,
        requested_by: str,
        traceparent: str,
    ) -> dict[str, Any]:
        _validate_traceparent(traceparent)
        _validate_reason_code(reason_code)
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must contain 1..256 characters")
        request_hash = _sha256({"run_id": run_id, "reason_code": reason_code})
        with self._connect(self.api_dsn) as conn:
            self._prepare(conn, tenant_id)
            workflow = self._workflow_row(conn, tenant_id, run_id, lock=True)
            by_key = conn.execute(
                """
                SELECT run_id, request_sha256
                FROM public.devops_run_cancellations
                WHERE tenant_id=%s AND idempotency_key=%s
                """,
                (tenant_id, idempotency_key),
            ).fetchone()
            if by_key is not None:
                if by_key[0] != run_id or by_key[1] != request_hash:
                    raise WorkflowIdempotencyConflictError(
                        "cancellation idempotency key was reused with a different request"
                    )
                return {
                    "accepted": True,
                    "status": workflow["status"],
                    "state_version": workflow["state_version"],
                    "cancel_epoch": workflow["cancel_epoch"],
                }

            existing = conn.execute(
                """
                SELECT idempotency_key, request_sha256
                FROM public.devops_run_cancellations
                WHERE tenant_id=%s AND run_id=%s
                """,
                (tenant_id, run_id),
            ).fetchone()
            if existing is not None:
                if existing[1] != request_hash:
                    raise WorkflowIdempotencyConflictError(
                        "workflow already has a different cancellation request"
                    )
                return {
                    "accepted": False,
                    "status": workflow["status"],
                    "state_version": workflow["state_version"],
                    "cancel_epoch": workflow["cancel_epoch"],
                }
            if workflow["status"] in _TERMINAL_RUN_STATUSES:
                return {
                    "accepted": False,
                    "status": workflow["status"],
                    "state_version": workflow["state_version"],
                    "cancel_epoch": workflow["cancel_epoch"],
                }
            if workflow["status"] == "cancel_requested":
                return {
                    "accepted": False,
                    "status": workflow["status"],
                    "state_version": workflow["state_version"],
                    "cancel_epoch": workflow["cancel_epoch"],
                }

            cancel_epoch = workflow["cancel_epoch"] + 1
            run_version = workflow["state_version"] + 1
            conn.execute(
                """
                UPDATE public.devops_workflows
                SET status='cancel_requested',
                    state_version=%s,
                    cancel_epoch=%s,
                    cancel_requested_at=clock_timestamp(),
                    cancel_reason_code=%s,
                    cancel_idempotency_key=%s,
                    cancel_request_sha256=%s,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND run_id=%s
                """,
                (
                    run_version,
                    cancel_epoch,
                    reason_code,
                    idempotency_key,
                    request_hash,
                    tenant_id,
                    run_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO public.devops_run_cancellations
                    (tenant_id, run_id, idempotency_key, request_sha256,
                     cancel_epoch, requested_by, reason_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    run_id,
                    idempotency_key,
                    request_hash,
                    cancel_epoch,
                    requested_by,
                    reason_code,
                ),
            )
            tasks = conn.execute(
                """
                SELECT task_id, attempt, state_version, status, lease_owner, lease_until
                FROM public.devops_tasks
                WHERE tenant_id=%s AND run_id=%s
                  AND status IN ('queued','accepted','running','validating','needs_approval')
                ORDER BY created_at
                FOR UPDATE
                """,
                (tenant_id, run_id),
            ).fetchall()
            prior_side_effects = conn.execute(
                """
                SELECT count(*)
                FROM public.devops_tasks
                WHERE tenant_id=%s AND run_id=%s
                  AND COALESCE(side_effect, 'NONE') <> 'NONE'
                """,
                (tenant_id, run_id),
            ).fetchone()[0]
            active_leases = 0
            reconciliation_required = prior_side_effects > 0
            now = datetime.now(timezone.utc)
            for task_id, attempt, old_version, old_status, lease_owner, old_lease_until in tasks:
                new_task_version = old_version + 1
                has_worker = (
                    old_status != "queued"
                    and lease_owner is not None
                    and _as_utc(old_lease_until) is not None
                    and _as_utc(old_lease_until) > now
                )
                if has_worker:
                    active_leases += 1
                    conn.execute(
                        """
                        UPDATE public.devops_tasks
                        SET status='cancel_requested', state_version=%s,
                            updated_at=clock_timestamp()
                        WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                        """,
                        (new_task_version, tenant_id, run_id, task_id),
                    )
                    correlation_id, default_trace = self._event_context(
                        conn, tenant_id, run_id, task_id
                    )
                    event = self._event(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        event_type="task.cancel.requested",
                        payload={
                            "cancel_epoch": cancel_epoch,
                            "reason_code": reason_code,
                        },
                        correlation_id=correlation_id,
                        traceparent=traceparent or default_trace,
                    )
                    self._append_event(
                        conn,
                        tenant_id=tenant_id,
                        event=event,
                        destination="commands",
                        aggregate_version=new_task_version,
                    )
                elif old_status in {"queued", "needs_approval"}:
                    conn.execute(
                        """
                        UPDATE public.devops_tasks
                        SET status='cancelled', state_version=%s, side_effect='NONE',
                            lease_owner=NULL, lease_claim_id=NULL, lease_until=NULL,
                            updated_at=clock_timestamp()
                        WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                        """,
                        (new_task_version, tenant_id, run_id, task_id),
                    )
                    correlation_id, _ = self._event_context(
                        conn, tenant_id, run_id, task_id
                    )
                    event = self._event(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        event_type="task.cancelled",
                        payload={
                            "cancel_epoch": cancel_epoch,
                            "state_version": new_task_version,
                            "side_effect": "NONE",
                        },
                        correlation_id=correlation_id,
                        traceparent=traceparent,
                    )
                    self._append_event(
                        conn,
                        tenant_id=tenant_id,
                        event=event,
                        destination="events",
                        aggregate_version=new_task_version,
                    )
                else:
                    reconciliation_required = True
                    conn.execute(
                        """
                        UPDATE public.devops_tasks
                        SET status='cancel_requested', state_version=%s,
                            side_effect='UNKNOWN', lease_owner=NULL, lease_claim_id=NULL,
                            lease_until=NULL,
                            updated_at=clock_timestamp()
                        WHERE tenant_id=%s AND run_id=%s AND task_id=%s
                        """,
                        (new_task_version, tenant_id, run_id, task_id),
                    )
                    correlation_id, _ = self._event_context(
                        conn, tenant_id, run_id, task_id
                    )
                    audit_event = self._event(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt=attempt,
                        event_type="audit.recorded",
                        payload={
                            "action": "task.cancel.reconciliation_required",
                            "outcome": "pending",
                            "resource_ref": f"task://{task_id}/{attempt}",
                            "state_version": new_task_version,
                        },
                        correlation_id=correlation_id,
                        traceparent=traceparent,
                    )
                    self._append_event(
                        conn,
                        tenant_id=tenant_id,
                        event=audit_event,
                        destination="events",
                        aggregate_version=new_task_version,
                    )

            if active_leases == 0 and not reconciliation_required:
                run_version += 1
                conn.execute(
                    """
                    UPDATE public.devops_workflows
                    SET status='cancelled', state_version=%s,
                        updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND run_id=%s
                    """,
                    (run_version, tenant_id, run_id),
                )
                conn.execute(
                    """
                    UPDATE public.devops_run_cancellations
                    SET status='completed', completed_at=clock_timestamp()
                    WHERE tenant_id=%s AND run_id=%s
                    """,
                    (tenant_id, run_id),
                )
                completion_event = self._event(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    task_id=None,
                    attempt=None,
                    event_type="audit.recorded",
                    payload={
                        "action": "workflow.cancelled",
                        "outcome": "cancelled",
                        "resource_ref": f"workflow://{run_id}",
                        "state_version": run_version,
                    },
                    correlation_id=run_id,
                    traceparent=traceparent,
                )
                self._append_event(
                    conn,
                    tenant_id=tenant_id,
                    event=completion_event,
                    destination="events",
                    aggregate_version=run_version,
                )
                status = "cancelled"
            else:
                status = "cancel_requested"
            if reconciliation_required:
                conn.execute(
                    """
                    UPDATE public.devops_run_cancellations
                    SET status='reconciliation_required'
                    WHERE tenant_id=%s AND run_id=%s AND status='requested'
                    """,
                    (tenant_id, run_id),
                )
            return {
                "accepted": True,
                "status": status,
                "state_version": run_version,
                "cancel_epoch": cancel_epoch,
            }
