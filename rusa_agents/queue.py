"""PostgreSQL-backed durable queue with leases, retries, and tenant scoping."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


class IdempotencyConflictError(ValueError):
    """An idempotency key was reused for a different tenant task payload."""


class LeaseLostError(RuntimeError):
    """A worker attempted a side effect without holding its current task lease."""


@dataclass(frozen=True)
class Job:
    id: int
    task_id: str
    tenant_id: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_token: str


class PostgresTaskQueue:
    """Multi-worker queue. PostgreSQL is the source of truth; SKIP LOCKED avoids double claims."""

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install the PostgreSQL runtime dependencies to use PostgresTaskQueue") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required for tenant-scoped database access")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    @staticmethod
    def _require_policy_audit_actor(actor: str | None) -> None:
        if os.getenv("ENVIRONMENT", "development").lower() == "production" and actor is None:
            raise RuntimeError("Production task queue operations require a policy audit actor")

    @staticmethod
    def _record_policy_event(
        conn,
        tenant_id: str,
        task_id: str,
        actor: str | None,
        action: str,
        decision: str,
    ) -> None:
        if actor is None:
            return
        if not isinstance(actor, str) or len(actor) > 128:
            raise ValueError("Task policy audit actor is invalid")
        if actor.startswith("api:"):
            if action not in {"task.create", "task.read", "task.retry", "task.cancel"}:
                raise ValueError("API task policy audit action is invalid")
        elif actor.startswith("worker:"):
            if action != "task.execute":
                raise ValueError("Worker task policy audit action is invalid")
        else:
            raise ValueError("Task policy audit actor is invalid")
        if decision not in {"allow", "deny"}:
            raise ValueError("Task policy audit decision is invalid")
        conn.execute(
            """
            INSERT INTO public.aicode_task_policy_events
                (tenant_id, task_id, actor, action, decision)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (tenant_id, task_id, actor, action, decision),
        )

    def initialize(self) -> None:
        with self._connect() as conn:
            if os.getenv("ENVIRONMENT", "development").lower() == "production":
                exists = conn.execute("SELECT to_regclass('public.aicode_jobs')").fetchone()
                if not exists or exists[0] is None:
                    raise RuntimeError("Production database schema is missing; apply migrations/001_aicode_core.sql")
                lease_column = conn.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='aicode_jobs'
                          AND column_name='lease_token'
                    )
                """).fetchone()
                if not lease_column or not lease_column[0]:
                    raise RuntimeError(
                        "Production queue lease fencing is not installed; "
                        "apply migrations/002_aicode_lease_fencing.sql"
                    )
                idempotency_column = conn.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='aicode_jobs'
                          AND column_name='idempotency_key'
                    )
                """).fetchone()
                if not idempotency_column or not idempotency_column[0]:
                    raise RuntimeError(
                        "Production idempotency migration is not installed; "
                        "apply migrations/006_aicode_idempotency.sql"
                    )
                idempotency_index = conn.execute(
                    "SELECT to_regclass('public.idx_aicode_jobs_idempotency')"
                ).fetchone()
                if not idempotency_index or idempotency_index[0] is None:
                    raise RuntimeError(
                        "Production idempotency index is missing; "
                        "apply migrations/006_aicode_idempotency.sql"
                    )
                result_column = conn.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='aicode_jobs'
                          AND column_name='result'
                    )
                """).fetchone()
                if not result_column or not result_column[0]:
                    raise RuntimeError(
                        "Production task result storage is not installed; "
                        "apply migrations/003_aicode_task_results.sql"
                    )
                protected = conn.execute("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid=c.relnamespace
                    WHERE n.nspname='public'
                      AND c.relname = ANY(%s)
                """, (["aicode_jobs", "aicode_rate_limits", "aicode_memory", "aicode_audit_events"],)).fetchall()
                states = {row[0]: (row[1], row[2]) for row in protected}
                if set(states) != {"aicode_jobs", "aicode_rate_limits", "aicode_memory", "aicode_audit_events"}:
                    raise RuntimeError("Production RLS migration is incomplete; apply migrations/004_aicode_tenant_rls.sql")
                if any(not enabled or not forced for enabled, forced in states.values()):
                    raise RuntimeError("Production RLS must be enabled and forced on all AICode tables")
                policies = conn.execute("""
                    SELECT tablename, policyname FROM pg_policies
                    WHERE schemaname='public' AND policyname = ANY(%s)
                """, ([
                    "aicode_jobs_api_tenant", "aicode_jobs_worker_queue",
                    "aicode_rate_limits_api_tenant", "aicode_memory_worker_tenant",
                    "aicode_audit_worker_only",
                ],)).fetchall()
                policy_names = {row[1] for row in policies}
                required_policies = {
                    "aicode_jobs_api_tenant", "aicode_jobs_worker_queue",
                    "aicode_rate_limits_api_tenant", "aicode_memory_worker_tenant",
                    "aicode_audit_worker_only",
                }
                if not required_policies.issubset(policy_names):
                    raise RuntimeError("Production tenant RLS policies are missing; apply migrations/004_aicode_tenant_rls.sql")

                roles = conn.execute("""
                    SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = ANY(%s)
                """, (["rusa_api_runtime", "rusa_worker_runtime"],)).fetchall()
                role_flags = {row[0]: (row[1], row[2], row[3]) for row in roles}
                if set(role_flags) != {"rusa_api_runtime", "rusa_worker_runtime"}:
                    raise RuntimeError("Separate API/worker database role groups are missing")
                if any(any(flags) for flags in role_flags.values()):
                    raise RuntimeError(
                        "API/worker role groups must be NOLOGIN, NOSUPERUSER, and NOBYPASSRLS"
                    )

                identity = conn.execute("""
                    SELECT current_user,
                           pg_has_role(current_user, 'rusa_api_runtime', 'member'),
                           pg_has_role(current_user, 'rusa_worker_runtime', 'member'),
                           COALESCE((
                               SELECT rolsuper OR rolbypassrls OR NOT rolcanlogin
                               FROM pg_roles WHERE rolname=current_user
                           ), true)
                """).fetchone()
                if (
                    not identity
                    or identity[3]
                    or identity[1] == identity[2]
                    or identity[0] not in {"rusa_api_login", "rusa_worker_login"}
                ):
                    raise RuntimeError(
                        "Database connection must use exactly one non-superuser API or worker login role"
                    )
                if (
                    identity[0] == "rusa_api_login" and not identity[1]
                ) or (
                    identity[0] == "rusa_worker_login" and not identity[2]
                ):
                    raise RuntimeError("Database login is not a member of its matching runtime role group")
                return
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aicode_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    task_id UUID NOT NULL,
                    tenant_id TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'awaiting_approval', 'succeeded', 'failed', 'cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
                    priority INTEGER NOT NULL DEFAULT 0,
                    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    lease_owner TEXT,
                    lease_until TIMESTAMPTZ,
                    lease_token UUID,
                    result JSONB,
                    idempotency_key TEXT,
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, task_id)
                )
            """)
            conn.execute("ALTER TABLE aicode_jobs ADD COLUMN IF NOT EXISTS lease_token UUID")
            conn.execute("ALTER TABLE aicode_jobs ADD COLUMN IF NOT EXISTS result JSONB")
            conn.execute("ALTER TABLE aicode_jobs ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_aicode_jobs_idempotency
                ON aicode_jobs(tenant_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_aicode_jobs_claim
                ON aicode_jobs(status, available_at, priority DESC, created_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_aicode_jobs_tenant
                ON aicode_jobs(tenant_id, created_at DESC)
            """)

    def ping(self) -> bool:
        """Return database connectivity without exposing application data."""
        with self._connect() as conn:
            row = conn.execute("SELECT 1").fetchone()
        return row is not None and row[0] == 1

    def enqueue(
        self,
        task_id: str,
        tenant_id: str,
        payload: dict[str, Any],
        max_attempts: int = 3,
        priority: int = 0,
        *,
        audit_actor: str | None = None,
    ) -> bool:
        if not tenant_id or not task_id or max_attempts < 1:
            raise ValueError("tenant_id, task_id, and positive max_attempts are required")
        self._require_policy_audit_actor(audit_actor)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute("""
                INSERT INTO aicode_jobs(task_id, tenant_id, payload, max_attempts, priority)
                VALUES (%s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (tenant_id, task_id) DO NOTHING
                RETURNING id
            """, (task_id, tenant_id, json.dumps(payload), max_attempts, priority)).fetchone()
            self._record_policy_event(
                conn, tenant_id, task_id, audit_actor, "task.create",
                "allow" if row is not None else "deny",
            )
        return row is not None

    def enqueue_idempotent(
        self,
        task_id: str,
        tenant_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = 3,
        priority: int = 0,
        *,
        audit_actor: str | None = None,
    ) -> tuple[str, bool, str]:
        """Create once per tenant/key; identical retries return the original task ID and state."""
        import re

        if (
            not isinstance(idempotency_key, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key)
        ):
            raise ValueError("Idempotency-Key must be 1-128 safe ASCII characters")
        if not tenant_id or not task_id or max_attempts < 1:
            raise ValueError("tenant_id, task_id, and positive max_attempts are required")
        if not isinstance(payload, dict):
            raise ValueError("task payload must be an object")
        self._require_policy_audit_actor(audit_actor)

        def comparable(value: dict[str, Any]) -> str:
            normalized = json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))
            repository = normalized.get("repository")
            if isinstance(repository, dict):
                repository.pop("work_branch_prefix", None)
            return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            inserted = conn.execute(
                """
                INSERT INTO aicode_jobs(
                    task_id, tenant_id, payload, max_attempts, priority, idempotency_key
                )
                VALUES (%s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING task_id::text, status
                """,
                (
                    task_id, tenant_id, json.dumps(payload, ensure_ascii=False),
                    max_attempts, priority, idempotency_key,
                ),
            ).fetchone()
            if inserted is not None:
                self._record_policy_event(
                    conn, tenant_id, task_id, audit_actor, "task.create", "allow"
                )
                return str(inserted[0]), True, str(inserted[1])

            existing = conn.execute(
                """
                SELECT task_id::text, payload, status
                FROM aicode_jobs
                WHERE tenant_id=%s AND idempotency_key=%s
                FOR UPDATE
                """,
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing is None:
                self._record_policy_event(
                    conn, tenant_id, task_id, audit_actor, "task.create", "deny"
                )
                raise IdempotencyConflictError("Task identifier conflicts with an existing task")
            existing_payload = existing[1] if isinstance(existing[1], dict) else json.loads(existing[1])
            if comparable(existing_payload) != comparable(payload):
                self._record_policy_event(
                    conn, tenant_id, str(existing[0]), audit_actor, "task.create", "deny"
                )
                raise IdempotencyConflictError(
                    "Idempotency-Key was already used with a different task payload"
                )
            self._record_policy_event(
                conn, tenant_id, str(existing[0]), audit_actor, "task.create", "allow"
            )
            return str(existing[0]), False, str(existing[2])

    def claim(self, worker_id: str, lease_seconds: int = 180) -> Job | None:
        if not worker_id or lease_seconds < 15 or lease_seconds > 3600:
            raise ValueError("worker_id is required and lease_seconds must be between 15 and 3600")
        lease_token = str(uuid4())
        with self._connect() as conn:
            conn.execute("""
                UPDATE aicode_jobs
                SET status='failed', lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                    last_error='Lease expired after maximum attempts', updated_at=NOW()
                WHERE status='running' AND lease_until < NOW() AND attempts >= max_attempts
            """)
            row = conn.execute("""
                WITH candidate AS (
                    SELECT id FROM aicode_jobs
                    WHERE attempts < max_attempts
                      AND ((status='queued' AND available_at <= NOW())
                        OR (status='running' AND lease_until < NOW()))
                    ORDER BY priority DESC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE aicode_jobs AS job
                SET status='running', attempts=job.attempts+1, lease_owner=%s,
                    lease_token=%s::uuid,
                    lease_until=NOW() + (%s * INTERVAL '1 second'), updated_at=NOW()
                FROM candidate
                WHERE job.id=candidate.id
                RETURNING job.id, job.task_id::text, job.tenant_id, job.payload,
                          job.attempts, job.max_attempts, job.lease_owner, job.lease_token::text
            """, (worker_id, lease_token, lease_seconds)).fetchone()
        if row is None:
            return None
        payload = row[3] if isinstance(row[3], dict) else json.loads(row[3])
        return Job(row[0], row[1], row[2], payload, row[4], row[5], row[6], row[7])

    def heartbeat(
        self, job_id: int, tenant_id: str, worker_id: str,
        lease_token: str, lease_seconds: int = 180,
    ) -> bool:
        if not lease_token or not 15 <= lease_seconds <= 3600:
            raise ValueError("A lease token and lease duration between 15 and 3600 seconds are required")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cur = conn.execute("""
                UPDATE aicode_jobs SET lease_until=NOW() + (%s * INTERVAL '1 second'),
                    updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running' AND lease_until > NOW()
            """, (lease_seconds, job_id, tenant_id, worker_id, lease_token))
        return cur.rowcount == 1

    def publish_if_lease_owned(
        self,
        job_id: int,
        tenant_id: str,
        worker_id: str,
        lease_token: str,
        publish: Callable[[], str | None],
        *,
        lease_seconds: int = 900,
    ) -> str | None:
        """Hold the queue row lock across remote publication to prevent lease handoff races.

        The bounded Git operation runs while the active lease row is locked. A replacement
        worker uses SKIP LOCKED and cannot reclaim this task until publication has finished.
        The lease is extended before publication and refreshed again before the transaction
        releases the row. The publisher must use a task/lease-specific branch.
        """
        if (
            type(job_id) is not int
            or job_id <= 0
            or not tenant_id
            or not worker_id
            or len(worker_id) > 128
            or not lease_token
            or not callable(publish)
            or not 15 <= lease_seconds <= 3600
        ):
            raise ValueError("Publication lease identity or duration is invalid")

        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            owned = conn.execute(
                """
                SELECT 1 FROM public.aicode_jobs
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running'
                  AND lease_until > NOW()
                FOR UPDATE
                """,
                (job_id, tenant_id, worker_id, lease_token),
            ).fetchone()
            if owned is None:
                raise LeaseLostError("Refusing repository publication without the active task lease")

            extended = conn.execute(
                """
                UPDATE public.aicode_jobs
                SET lease_until=NOW() + (%s * INTERVAL '1 second'),
                    updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running'
                  AND lease_until > NOW()
                RETURNING id
                """,
                (lease_seconds, job_id, tenant_id, worker_id, lease_token),
            ).fetchone()
            if extended is None:
                raise LeaseLostError("Task lease expired before repository publication")

            # Keep this transaction/row lock open while the bounded Git push runs.
            # Queue.claim() uses FOR UPDATE SKIP LOCKED, so a replacement worker
            # cannot take ownership between this check and the remote side effect.
            result = publish()

            refreshed = conn.execute(
                """
                UPDATE public.aicode_jobs
                SET lease_until=NOW() + (180 * INTERVAL '1 second'),
                    updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running'
                RETURNING id
                """,
                (job_id, tenant_id, worker_id, lease_token),
            ).fetchone()
            if refreshed is None:
                raise LeaseLostError("Task lease changed during repository publication")
            return result

    def complete(
        self,
        job_id: int,
        tenant_id: str,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cur = conn.execute("""
                UPDATE aicode_jobs SET status='succeeded', result=%s::jsonb,
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running' AND lease_until > NOW()
            """, (
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                job_id, tenant_id, worker_id, lease_token,
            ))
        return cur.rowcount == 1

    def await_approval(
        self,
        job_id: int,
        tenant_id: str,
        worker_id: str,
        lease_token: str,
        reason: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cur = conn.execute("""
                UPDATE aicode_jobs SET status='awaiting_approval', result=%s::jsonb,
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                    last_error=%s, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running' AND lease_until > NOW()
            """, (
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                reason[:4000], job_id, tenant_id, worker_id, lease_token,
            ))
        return cur.rowcount == 1

    def fail(
        self, job_id: int, tenant_id: str, worker_id: str,
        lease_token: str, error: str, retry_delay_seconds: int = 30,
    ) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cur = conn.execute("""
                UPDATE aicode_jobs
                SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                    available_at=NOW() + (%s * INTERVAL '1 second'),
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                    last_error=%s, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running' AND lease_until > NOW()
            """, (
                max(0, retry_delay_seconds), error[:4000],
                job_id, tenant_id, worker_id, lease_token,
            ))
        return cur.rowcount == 1


    def authorize_execution(
        self,
        job_id: int,
        task_id: str,
        tenant_id: str,
        worker_id: str,
        lease_token: str,
    ) -> bool:
        """Atomically verify a live tenant-bound lease and append its policy decision."""
        if (
            type(job_id) is not int
            or job_id <= 0
            or not task_id
            or not tenant_id
            or not worker_id
            or len(worker_id) > 128
            or not lease_token
        ):
            raise ValueError("Worker lease identity is invalid")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT 1 FROM aicode_jobs
                WHERE id=%s AND task_id=%s AND tenant_id=%s
                  AND lease_owner=%s AND lease_token=%s::uuid
                  AND status='running' AND lease_until > NOW()
                FOR UPDATE
                """,
                (job_id, task_id, tenant_id, worker_id, lease_token),
            ).fetchone()
            self._record_policy_event(
                conn,
                tenant_id,
                task_id,
                f"worker:{worker_id}",
                "task.execute",
                "allow" if row is not None else "deny",
            )
        return row is not None

    def owns_lease(
        self, job_id: int, tenant_id: str, worker_id: str, lease_token: str,
    ) -> bool:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM aicode_jobs
                    WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                      AND lease_token=%s::uuid AND status='running'
                      AND lease_until > NOW()
                )
            """, (job_id, tenant_id, worker_id, lease_token)).fetchone()
        return bool(row and row[0])

    def retry(
        self, task_id: str, tenant_id: str, *, audit_actor: str | None = None,
    ) -> bool:
        """Explicitly requeue a failed or approval-paused task for a tenant."""
        if not task_id or not tenant_id:
            raise ValueError("task_id and tenant_id are required")
        self._require_policy_audit_actor(audit_actor)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute("""
                UPDATE aicode_jobs
                SET status='queued', attempts=0, available_at=NOW(), result=NULL,
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL, last_error=NULL, updated_at=NOW()
                WHERE task_id=%s AND tenant_id=%s
                  AND status IN ('awaiting_approval', 'failed')
                RETURNING id
            """, (task_id, tenant_id)).fetchone()
            self._record_policy_event(
                conn, tenant_id, task_id, audit_actor, "task.retry",
                "allow" if row is not None else "deny",
            )
        return row is not None

    def cancel(
        self, task_id: str, tenant_id: str, *, audit_actor: str | None = None,
    ) -> bool:
        self._require_policy_audit_actor(audit_actor)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cur = conn.execute("""
                UPDATE aicode_jobs SET status='cancelled', lease_owner=NULL,
                    lease_until=NULL, lease_token=NULL, updated_at=NOW()
                WHERE task_id=%s AND tenant_id=%s AND status IN ('queued', 'running', 'awaiting_approval')
            """, (task_id, tenant_id))
            self._record_policy_event(
                conn, tenant_id, task_id, audit_actor, "task.cancel",
                "allow" if cur.rowcount == 1 else "deny",
            )
        return cur.rowcount == 1

    def get(
        self, task_id: str, tenant_id: str, *, audit_actor: str | None = None,
    ) -> dict[str, Any] | None:
        self._require_policy_audit_actor(audit_actor)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute("""
                SELECT task_id::text, tenant_id, payload, result, status, attempts,
                       max_attempts, last_error, created_at, updated_at
                FROM aicode_jobs WHERE task_id=%s AND tenant_id=%s
            """, (task_id, tenant_id)).fetchone()
            self._record_policy_event(
                conn, tenant_id, task_id, audit_actor, "task.read",
                "allow" if row is not None else "deny",
            )
        if row is None:
            return None
        payload = row[2] if isinstance(row[2], dict) else json.loads(row[2])
        result = row[3] if isinstance(row[3], dict) else (
            json.loads(row[3]) if row[3] is not None else None
        )
        return {
            "task_id": row[0], "tenant_id": row[1], "payload": payload,
            "result": result, "status": row[4], "attempts": row[5],
            "max_attempts": row[6], "last_error": row[7],
            "created_at": row[8].isoformat(), "updated_at": row[9].isoformat(),
        }
