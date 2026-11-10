"""Tenant-bound human approval records with immutable bindings and one-time consumption."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .platform_contracts import WorkflowEventV2
from .workflow_store import PostgresWorkflowStore


class ApprovalError(RuntimeError):
    """Approval request, decision, expiry, or consumption failed."""


ApproverAuthorizer = Callable[[str, str, str, str], bool]


def _binding_digest(
    *,
    tenant_id: str,
    workflow_id: str,
    run_id: str,
    task_id: str,
    action: str,
    resource_ref: str,
    artifact_digest: str | None,
    policy_id: str,
    policy_version: str,
    expires_at: datetime,
) -> str:
    value = {
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "task_id": task_id,
        "action": action,
        "resource_ref": resource_ref,
        "artifact_digest": artifact_digest,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PostgresHumanApprovalService:
    """Backend-only approval contract. No browser/UI approval endpoint is introduced here."""

    def __init__(
        self,
        dsn: str,
        *,
        approver_authorizer: ApproverAuthorizer,
        consumer_authorizer: ApproverAuthorizer | None = None,
    ):
        if not dsn or not callable(approver_authorizer):
            raise ValueError("PostgreSQL DSN and fail-closed approver authorizer are required")
        if consumer_authorizer is not None and not callable(consumer_authorizer):
            raise ValueError("approval consumer authorizer must be callable")
        self.dsn = dsn
        self.approver_authorizer = approver_authorizer
        self.consumer_authorizer = consumer_authorizer

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install PostgreSQL runtime dependencies to use approval records") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required for approval access")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def request(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        run_id: str,
        task_id: str,
        requested_by: str,
        action: str,
        resource_ref: str,
        reason: str,
        policy_id: str,
        policy_version: str,
        expires_at: datetime,
        artifact_digest: str | None = None,
        attempt: int = 1,
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("approval expiry must be timezone-aware")
        expires = expires_at.astimezone(timezone.utc)
        if not now < expires <= now + timedelta(days=30):
            raise ValueError("approval expiry must be in the next 30 days")
        required = (tenant_id, workflow_id, run_id, task_id, requested_by, action, resource_ref, reason, policy_id, policy_version)
        if any(not isinstance(value, str) or not value.strip() for value in required):
            raise ValueError("approval identity, action, resource, reason, and policy fields are required")
        if len(reason) > 2000 or not 1 <= attempt <= 100:
            raise ValueError("approval reason or attempt is outside the allowed range")
        if artifact_digest is not None:
            import re
            if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", artifact_digest):
                raise ValueError("approval artifact digest must be a SHA-256 digest")

        approval_id = str(uuid4())
        binding = _binding_digest(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            action=action,
            resource_ref=resource_ref,
            artifact_digest=artifact_digest,
            policy_id=policy_id,
            policy_version=policy_version,
            expires_at=expires,
        )
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            task = conn.execute(
                """
                SELECT 1 FROM public.rusa_workflow_tasks
                WHERE tenant_id=%s AND workflow_id=%s AND run_id=%s AND task_id=%s
                """,
                (tenant_id, workflow_id, run_id, task_id),
            ).fetchone()
            if task is None:
                raise ApprovalError("approval must bind to an existing tenant workflow task")
            conn.execute(
                """
                INSERT INTO public.rusa_human_approvals(
                    tenant_id, approval_id, workflow_id, run_id, task_id,
                    action, resource_ref, artifact_digest, binding_sha256,
                    policy_id, policy_version, requested_by, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id, approval_id, workflow_id, run_id, task_id,
                    action, resource_ref, artifact_digest, binding,
                    policy_id, policy_version, requested_by, expires,
                ),
            )
            event = WorkflowEventV2(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                correlation_id=workflow_id,
                traceparent=f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
                event_type="approval.requested",
                producer_id="rusa.approval_service",
                producer_version="1.0.0",
                occurred_at=now,
                payload={
                    "approval_id": approval_id,
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "requested_by": requested_by,
                    "action": action,
                    "resource_ref": resource_ref,
                    "artifact_digest": artifact_digest,
                    "binding_sha256": binding,
                    "reason": reason,
                    "policy_id": policy_id,
                    "policy_version": policy_version,
                    "expires_at": expires.isoformat(),
                },
            )
            PostgresWorkflowStore._append_event(conn, event)
        return {"approval_id": approval_id, "binding_sha256": binding, "status": "pending"}

    def decide(
        self,
        tenant_id: str,
        approval_id: str,
        *,
        actor: str,
        decision: str,
        reason: str | None = None,
    ) -> dict[str, str]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("human decision must be approved or rejected")
        if not actor or len(actor) > 128 or (reason is not None and len(reason) > 2000):
            raise ValueError("approver identity or decision reason is invalid")
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT workflow_id, run_id, task_id, action, resource_ref,
                       artifact_digest, binding_sha256, policy_id, policy_version,
                       requested_by, status, expires_at
                FROM public.rusa_human_approvals
                WHERE tenant_id=%s AND approval_id=%s
                FOR UPDATE
                """,
                (tenant_id, approval_id),
            ).fetchone()
            if row is None:
                raise ApprovalError("approval record was not found")
            if row[10] != "pending":
                raise ApprovalError("approval is already decided or terminal")
            if row[11] <= now:
                self._mark_expired(
                    conn, tenant_id, approval_id,
                    row[0], row[1], row[2], row[7], row[8], now,
                )
                expired = True
            else:
                expired = False
            if not expired and actor == row[9]:
                raise PermissionError("approval requester cannot approve their own request")
            if not expired and not self.approver_authorizer(actor, tenant_id, row[3], row[4]):
                raise PermissionError("actor is not authorized to decide this approval")

            if expired:
                pass
            else:
                expected = _binding_digest(
                    tenant_id=tenant_id,
                    workflow_id=row[0],
                    run_id=row[1],
                    task_id=row[2],
                    action=row[3],
                    resource_ref=row[4],
                    artifact_digest=row[5],
                    policy_id=row[7],
                    policy_version=row[8],
                    expires_at=row[11],
                )
                if expected != row[6]:
                    raise ApprovalError("approval binding digest does not match its immutable request")

                conn.execute(
                    """
                    UPDATE public.rusa_human_approvals
                    SET status=%s, decided_by=%s, decided_at=%s,
                        decision_reason=%s, updated_at=%s
                    WHERE tenant_id=%s AND approval_id=%s AND status='pending'
                    """,
                    (decision, actor, now, reason, now, tenant_id, approval_id),
                )
                event = WorkflowEventV2(
                    tenant_id=tenant_id,
                    workflow_id=row[0],
                    run_id=row[1],
                    task_id=row[2],
                    correlation_id=row[0],
                    traceparent=f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
                    event_type="approval.decided",
                    producer_id="rusa.approval_service",
                    producer_version="1.0.0",
                    occurred_at=now,
                    payload={
                        "approval_id": approval_id,
                        "decision": decision,
                        "decided_by": actor,
                        "decided_at": now.isoformat(),
                        "reason": reason,
                        "policy_id": row[7],
                        "policy_version": row[8],
                    },
                )
                PostgresWorkflowStore._append_event(conn, event)
        if expired:
            raise ApprovalError("approval has expired")
        return {"approval_id": approval_id, "status": decision}

    @staticmethod
    def _mark_expired(
        conn,
        tenant_id: str,
        approval_id: str,
        workflow_id: str,
        run_id: str,
        task_id: str,
        policy_id: str,
        policy_version: str,
        now: datetime,
    ) -> None:
        conn.execute(
            """
            UPDATE public.rusa_human_approvals
            SET status='expired', decided_by='system:approval-expirer',
                decided_at=%s, decision_reason='approval_expired', updated_at=%s
            WHERE tenant_id=%s AND approval_id=%s AND status='pending'
            """,
            (now, now, tenant_id, approval_id),
        )
        event = WorkflowEventV2(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            correlation_id=workflow_id,
            traceparent=f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
            event_type="approval.decided",
            producer_id="rusa.approval_service",
            producer_version="1.0.0",
            occurred_at=now,
            payload={
                "approval_id": approval_id,
                "decision": "expired",
                "decided_by": "system:approval-expirer",
                "decided_at": now.isoformat(),
                "reason": "approval_expired",
                "policy_id": policy_id,
                "policy_version": policy_version,
            },
        )
        PostgresWorkflowStore._append_event(conn, event)

    def expire_due(self, tenant_id: str, *, limit: int = 100) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("approval expiry batch limit must be between 1 and 500")
        now = datetime.now(timezone.utc)
        expired = 0
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT approval_id, workflow_id, run_id, task_id, action,
                       resource_ref, artifact_digest, binding_sha256,
                       policy_id, policy_version, requested_by, status, expires_at
                FROM public.rusa_human_approvals
                WHERE tenant_id=%s AND status='pending' AND expires_at <= %s
                ORDER BY expires_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (tenant_id, now, limit),
            ).fetchall()
            for row in rows:
                self._mark_expired(
                    conn, tenant_id, row[0], row[1], row[2], row[3], row[8], row[9], now,
                )
                expired += 1
        return expired

    def consume(
        self,
        tenant_id: str,
        approval_id: str,
        *,
        actor: str,
        action: str,
        resource_ref: str,
        workflow_id: str,
        run_id: str,
        task_id: str,
        artifact_digest: str | None = None,
    ) -> bool:
        """Consume an approved decision once and only for the exact original action/resource."""
        if not actor or len(actor) > 128:
            raise ValueError("approval consumer identity is invalid")
        if self.consumer_authorizer is None:
            raise PermissionError("approval consumption requires a configured task-execution authorizer")
        if not all((tenant_id, approval_id, action, resource_ref, workflow_id, run_id, task_id)):
            raise ValueError("approval consumption must include exact tenant, task, action, and resource identity")
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT a.workflow_id, a.run_id, a.task_id, a.action, a.resource_ref,
                       a.artifact_digest, a.binding_sha256, a.policy_id, a.policy_version,
                       a.expires_at, a.status
                FROM public.rusa_human_approvals a
                WHERE a.tenant_id=%s AND a.approval_id=%s
                  AND EXISTS (
                      SELECT 1 FROM public.rusa_workflow_runs r
                      WHERE r.tenant_id=a.tenant_id AND r.run_id=a.run_id
                        AND r.state='running' AND r.deadline_at > clock_timestamp()
                  )
                FOR UPDATE
                """,
                (tenant_id, approval_id),
            ).fetchone()
            if row is None:
                raise ApprovalError("approval record was not found")
            if row[10] != "approved" or row[9] <= now:
                raise ApprovalError("approval is not approved or has expired")
            expected = _binding_digest(
                tenant_id=tenant_id,
                workflow_id=row[0],
                run_id=row[1],
                task_id=row[2],
                action=row[3],
                resource_ref=row[4],
                artifact_digest=row[5],
                policy_id=row[7],
                policy_version=row[8],
                expires_at=row[9],
            )
            requested = _binding_digest(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                run_id=run_id,
                task_id=task_id,
                action=action,
                resource_ref=resource_ref,
                artifact_digest=artifact_digest,
                policy_id=row[7],
                policy_version=row[8],
                expires_at=row[9],
            )
            if expected != row[6] or requested != row[6]:
                raise ApprovalError("approval cannot be replayed for a different action, task, resource, or artifact")
            if not self.consumer_authorizer(actor, tenant_id, action, resource_ref):
                raise PermissionError("actor is not authorized to consume this approval for execution")
            inserted = conn.execute(
                """
                INSERT INTO public.rusa_approval_consumptions(
                    tenant_id, approval_id, binding_sha256, consumed_by
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, approval_id) DO NOTHING
                RETURNING approval_id
                """,
                (tenant_id, approval_id, expected, actor),
            ).fetchone()
            if inserted is None:
                return False
            event = WorkflowEventV2(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                run_id=run_id,
                task_id=task_id,
                correlation_id=workflow_id,
                traceparent=f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
                event_type="approval.consumed",
                producer_id="rusa.approval_service",
                producer_version="1.0.0",
                occurred_at=now,
                payload={
                    "approval_id": approval_id,
                    "consumed_by": actor,
                    "binding_sha256": expected,
                    "action": action,
                    "resource_ref": resource_ref,
                    "artifact_digest": artifact_digest,
                },
            )
            PostgresWorkflowStore._append_event(conn, event)
            return True
