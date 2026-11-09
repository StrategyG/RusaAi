"""Dedicated tenant-scoped PostgreSQL queue for DevOps jobs; does not consume AICode jobs."""
from __future__ import annotations

import json
import os
import re
from typing import Any
from uuid import uuid4

from .contracts import DevOpsJob


class PostgresDevOpsQueue:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.getenv("RUSA_WORKER_DATABASE_URL") or os.getenv("RUSA_API_DATABASE_URL") or os.getenv("RUSA_DATABASE_URL")
        if not self.dsn:
            raise ValueError("A DevOps PostgreSQL DSN is required")

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use the DevOps queue") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tenant_id or ""):
            raise ValueError("tenant_id is invalid")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def verify_ready(self) -> bool:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    to_regclass('public.rusa_devops_jobs') IS NOT NULL,
                    to_regclass('public.rusa_devops_audit_events') IS NOT NULL,
                    has_table_privilege(current_user, 'public.rusa_devops_jobs', 'SELECT'),
                    has_table_privilege(current_user, 'public.rusa_devops_jobs', 'UPDATE'),
                    (SELECT relrowsecurity AND relforcerowsecurity
                     FROM pg_class WHERE oid=to_regclass('public.rusa_devops_jobs')),
                    (SELECT pg_has_role(current_user, 'rusa_api_runtime', 'member')
                         OR pg_has_role(current_user, 'rusa_worker_runtime', 'member'))
                """
            ).fetchone()
        return bool(rows and all(rows))

    def ping(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    def enqueue(
        self, task_id: str, tenant_id: str, job_type: str, payload: dict[str, Any], *,
        max_attempts: int = 3, idempotency_key: str | None = None, audit_actor: str = "api:unknown",
    ) -> tuple[str, bool, str]:
        allowed = {
            "synthetic_ingest", "prometheus_collect", "train_forecaster",
            "anomaly_scan", "capacity_recommendation",
        }
        if job_type not in allowed:
            raise ValueError("unsupported DevOps job type")
        if not isinstance(payload, dict) or len(json.dumps(payload, allow_nan=False).encode("utf-8")) > 64_000:
            raise ValueError("job payload must be a JSON object up to 64 KiB")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", idempotency_key):
            raise ValueError("idempotency key must be 8-128 safe characters")
        task_id = task_id or str(uuid4())
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            if idempotency_key:
                row = conn.execute(
                    """
                    INSERT INTO public.rusa_devops_jobs
                        (task_id, tenant_id, job_type, payload, max_attempts, idempotency_key)
                    VALUES (%s::uuid, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING task_id, status
                    """,
                    (task_id, tenant_id, job_type, json.dumps(payload, allow_nan=False), max_attempts, idempotency_key),
                ).fetchone()
                if row is None:
                    existing = conn.execute(
                        """
                        SELECT task_id, status, job_type, payload
                        FROM public.rusa_devops_jobs WHERE tenant_id=%s AND idempotency_key=%s
                        """, (tenant_id, idempotency_key),
                    ).fetchone()
                    if existing is None or existing[2] != job_type or existing[3] != payload:
                        raise ValueError("idempotency key was reused with a different DevOps job")
                    return str(existing[0]), False, str(existing[1])
            else:
                row = conn.execute(
                    """
                    INSERT INTO public.rusa_devops_jobs
                        (task_id, tenant_id, job_type, payload, max_attempts)
                    VALUES (%s::uuid, %s, %s, %s::jsonb, %s)
                    RETURNING task_id, status
                    """,
                    (task_id, tenant_id, job_type, json.dumps(payload, allow_nan=False), max_attempts),
                ).fetchone()
            conn.execute(
                """
                INSERT INTO public.rusa_devops_audit_events
                    (tenant_id, task_id, actor, action, decision, details)
                VALUES (%s, %s, %s, 'job.enqueue', 'allow', %s::jsonb)
                """,
                (tenant_id, str(row[0]), audit_actor[:128], json.dumps({"job_type": job_type})),
            )
            return str(row[0]), True, str(row[1])

    @staticmethod
    def _row(row) -> dict[str, Any]:
        return {
            "task_id": str(row[0]), "tenant_id": row[1], "job_type": row[2],
            "payload": row[3], "status": row[4], "attempts": row[5],
            "max_attempts": row[6], "last_error": row[7], "result": row[8],
            "created_at": row[9].isoformat() if row[9] else None,
            "updated_at": row[10].isoformat() if row[10] else None,
        }

    def get(self, task_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT task_id, tenant_id, job_type, payload, status, attempts,
                       max_attempts, last_error, result, created_at, updated_at
                FROM public.rusa_devops_jobs WHERE task_id=%s::uuid AND tenant_id=%s
                """, (task_id, tenant_id),
            ).fetchone()
        return self._row(row) if row else None

    def list(self, tenant_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100_000:
            raise ValueError("pagination is outside allowed bounds")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT task_id, tenant_id, job_type, payload, status, attempts,
                       max_attempts, last_error, result, created_at, updated_at
                FROM public.rusa_devops_jobs
                WHERE tenant_id=%s AND (%s::text IS NULL OR status=%s)
                ORDER BY created_at DESC LIMIT %s OFFSET %s
                """, (tenant_id, status, status, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM public.rusa_devops_jobs WHERE tenant_id=%s AND (%s::text IS NULL OR status=%s)",
                (tenant_id, status, status),
            ).fetchone()[0]
        return {"items": [self._row(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}

    def overview(self, tenant_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM public.rusa_devops_jobs WHERE tenant_id=%s GROUP BY status",
                (tenant_id,),
            ).fetchall()
        counts = {str(status): int(count) for status, count in rows}
        return {
            "enabled": True, "autonomy_mode": "controlled", "queue": counts,
            "active_jobs": counts.get("queued", 0) + counts.get("running", 0),
            "approval_required": True, "infrastructure_execution_enabled": False,
        }

    def claim(self, worker_id: str, lease_seconds: int = 180) -> DevOpsJob | None:
        if not worker_id or not 15 <= lease_seconds <= 3600:
            raise ValueError("worker_id and a lease between 15 and 3600 seconds are required")
        lease_token = str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE public.rusa_devops_jobs
                SET status='failed', lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                    last_error='Lease expired after maximum attempts', updated_at=NOW()
                WHERE status='running' AND lease_until < NOW() AND attempts >= max_attempts
                """
            )
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT id FROM public.rusa_devops_jobs
                    WHERE (status='queued' AND available_at <= NOW())
                       OR (status='running' AND lease_until < NOW() AND attempts < max_attempts)
                    ORDER BY priority DESC, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE public.rusa_devops_jobs AS job
                SET status='running', attempts=job.attempts+1, lease_owner=%s,
                    lease_until=NOW() + (%s * INTERVAL '1 second'),
                    lease_token=%s::uuid, updated_at=NOW()
                FROM candidate WHERE job.id=candidate.id
                RETURNING job.id, job.task_id, job.tenant_id, job.job_type,
                          job.payload, job.attempts, job.max_attempts,
                          job.lease_owner, job.lease_token
                """, (worker_id, lease_seconds, lease_token),
            ).fetchone()
        if row is None:
            return None
        return DevOpsJob(
            id=int(row[0]), task_id=str(row[1]), tenant_id=row[2], job_type=row[3],
            payload=row[4], attempts=int(row[5]), max_attempts=int(row[6]),
            lease_owner=row[7], lease_token=str(row[8]),
        )

    def complete(self, job: DevOpsJob, result: dict[str, Any]) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, job.tenant_id)
            cursor = conn.execute(
                """
                UPDATE public.rusa_devops_jobs
                SET status='succeeded', result=%s::jsonb, lease_owner=NULL,
                    lease_until=NULL, lease_token=NULL, updated_at=NOW()
                WHERE id=%s AND task_id=%s::uuid AND tenant_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s::uuid
                  AND lease_until > NOW()
                """,
                (json.dumps(result, allow_nan=False), job.id, job.task_id, job.tenant_id, job.lease_owner, job.lease_token),
            )
            return cursor.rowcount == 1

    def fail(self, job: DevOpsJob, error: str, retry_delay_seconds: int = 30) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, job.tenant_id)
            cursor = conn.execute(
                """
                UPDATE public.rusa_devops_jobs
                SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                    available_at=NOW() + (%s * INTERVAL '1 second'),
                    last_error=%s, lease_owner=NULL, lease_until=NULL,
                    lease_token=NULL, updated_at=NOW()
                WHERE id=%s AND task_id=%s::uuid AND tenant_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s::uuid
                """,
                (max(1, min(retry_delay_seconds, 3600)), error[:4000], job.id, job.task_id, job.tenant_id, job.lease_owner, job.lease_token),
            )
            return cursor.rowcount == 1

    def retry(self, task_id: str, tenant_id: str, audit_actor: str) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cursor = conn.execute(
                """
                UPDATE public.rusa_devops_jobs
                SET status='queued', attempts=0, available_at=NOW(), last_error=NULL,
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL, updated_at=NOW()
                WHERE task_id=%s::uuid AND tenant_id=%s AND status='failed'
                """, (task_id, tenant_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    """
                    INSERT INTO public.rusa_devops_audit_events
                        (tenant_id, task_id, actor, action, decision, details)
                    VALUES (%s, %s, %s, 'job.retry', 'allow', '{}'::jsonb)
                    """, (tenant_id, task_id, audit_actor[:128]),
                )
            return cursor.rowcount == 1

    def cancel(self, task_id: str, tenant_id: str, audit_actor: str) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cursor = conn.execute(
                """
                UPDATE public.rusa_devops_jobs
                SET status='cancelled', lease_owner=NULL, lease_until=NULL,
                    lease_token=NULL, updated_at=NOW()
                WHERE task_id=%s::uuid AND tenant_id=%s AND status IN ('queued','running')
                """, (task_id, tenant_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    """
                    INSERT INTO public.rusa_devops_audit_events
                        (tenant_id, task_id, actor, action, decision, details)
                    VALUES (%s, %s, %s, 'job.cancel', 'allow', '{}'::jsonb)
                    """, (tenant_id, task_id, audit_actor[:128]),
                )
            return cursor.rowcount == 1
