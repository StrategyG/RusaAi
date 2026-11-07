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
    def _lock_execution_control(conn, job_id: int, tenant_id: str):
        """Serialize terminal transitions with cancellation using a transaction advisory lock.

        Advisory locks avoid granting the worker UPDATE rights on the API-owned control table.
        They are independent from the job row lock held during bounded Git publication.
        """
        task = conn.execute(
            "SELECT task_id::text FROM public.aicode_jobs WHERE id=%s AND tenant_id=%s",
            (job_id, tenant_id),
        ).fetchone()
        if task is None:
            return None
        lock_key = f"{tenant_id}:{task[0]}"
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_key,),
        )
        return conn.execute(
            """
            SELECT cancel_requested_at
            FROM public.aicode_job_controls
            WHERE tenant_id=%s AND task_id=%s
            """,
            (tenant_id, task[0]),
        ).fetchone()

    @staticmethod
    def _lock_task_control(conn, task_id: str, tenant_id: str) -> str | None:
        """Acquire the same advisory lock used by worker terminal transitions.

        Resolve the UUID from PostgreSQL first so alternate textual UUID spellings
        cannot bypass serialization between API cancellation/retry and worker ACKs.
        """
        row = conn.execute(
            """
            SELECT task_id::text
            FROM public.aicode_jobs
            WHERE tenant_id=%s AND task_id=%s
            """,
            (tenant_id, task_id),
        ).fetchone()
        if row is None:
            return None
        canonical_task_id = row[0]
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{tenant_id}:{canonical_task_id}",),
        )
        return canonical_task_id

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
                retry_columns = conn.execute("""
                    SELECT COUNT(*) = 2
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='aicode_jobs'
                      AND column_name = ANY(%s)
                """, (["manual_retry_count", "max_manual_retries"],)).fetchone()
                if not retry_columns or not retry_columns[0]:
                    raise RuntimeError(
                        "Production bounded manual retry policy is missing; "
                        "apply migrations/008_aicode_job_controls.sql"
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
                control_table = conn.execute(
                    "SELECT to_regclass('public.aicode_job_controls')"
                ).fetchone()
                if not control_table or control_table[0] is None:
                    raise RuntimeError(
                        "Production cancellation control is missing; "
                        "apply migrations/008_aicode_job_controls.sql"
                    )
                protected = conn.execute("""
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid=c.relnamespace
                    WHERE n.nspname='public'
                      AND c.relname = ANY(%s)
                """, ([
                    "aicode_jobs", "aicode_job_controls", "aicode_rate_limits",
                    "aicode_memory", "aicode_audit_events",
                ],)).fetchall()
                states = {row[0]: (row[1], row[2]) for row in protected}
                if set(states) != {
                    "aicode_jobs", "aicode_job_controls", "aicode_rate_limits",
                    "aicode_memory", "aicode_audit_events",
                }:
                    raise RuntimeError(
                        "Production RLS migration is incomplete; apply migrations/008_aicode_job_controls.sql"
                    )
                if any(not enabled or not forced for enabled, forced in states.values()):
                    raise RuntimeError("Production RLS must be enabled and forced on all AICode tables")
                policies = conn.execute("""
                    SELECT tablename, policyname FROM pg_policies
                    WHERE schemaname='public' AND policyname = ANY(%s)
                """, ([
                    "aicode_jobs_api_tenant", "aicode_jobs_worker_queue",
                    "aicode_job_controls_api_tenant", "aicode_job_controls_worker_read",
                    "aicode_rate_limits_api_tenant", "aicode_memory_worker_tenant",
                    "aicode_audit_worker_only",
                ],)).fetchall()
                policy_names = {row[1] for row in policies}
                required_policies = {
                    "aicode_jobs_api_tenant", "aicode_jobs_worker_queue",
                    "aicode_job_controls_api_tenant", "aicode_job_controls_worker_read",
                    "aicode_rate_limits_api_tenant", "aicode_memory_worker_tenant",
                    "aicode_audit_worker_only",
                }
                if not required_policies.issubset(policy_names):
                    raise RuntimeError(
                        "Production tenant/cancellation RLS policies are missing; "
                        "apply migrations/008_aicode_job_controls.sql"
                    )

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
            conn.execute(
                "ALTER TABLE aicode_jobs ADD COLUMN IF NOT EXISTS manual_retry_count "
                "INTEGER NOT NULL DEFAULT 0 CHECK (manual_retry_count >= 0)"
            )
            conn.execute(
                "ALTER TABLE aicode_jobs ADD COLUMN IF NOT EXISTS max_manual_retries "
                "INTEGER NOT NULL DEFAULT 3 CHECK (max_manual_retries >= 0)"
            )
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aicode_job_controls (
                    tenant_id TEXT NOT NULL,
                    task_id UUID NOT NULL,
                    cancel_epoch BIGINT NOT NULL DEFAULT 0 CHECK (cancel_epoch >= 0),
                    cancel_requested_at TIMESTAMPTZ,
                    requested_by TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, task_id),
                    FOREIGN KEY (tenant_id, task_id)
                        REFERENCES aicode_jobs(tenant_id, task_id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                INSERT INTO aicode_job_controls (tenant_id, task_id)
                SELECT tenant_id, task_id FROM aicode_jobs
                ON CONFLICT (tenant_id, task_id) DO NOTHING
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
            if row is not None:
                conn.execute(
                    """
                    INSERT INTO public.aicode_job_controls (tenant_id, task_id)
                    VALUES (%s, %s)
                    ON CONFLICT (tenant_id, task_id) DO NOTHING
                    """,
                    (tenant_id, task_id),
                )
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
                conn.execute(
                    """
                    INSERT INTO public.aicode_job_controls (tenant_id, task_id)
                    VALUES (%s, %s)
                    ON CONFLICT (tenant_id, task_id) DO NOTHING
                    """,
                    (tenant_id, task_id),
                )
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
                WITH cancelled AS (
                    SELECT job.id
                    FROM public.aicode_jobs AS job
                    JOIN public.aicode_job_controls AS control
                      ON control.tenant_id=job.tenant_id AND control.task_id=job.task_id
                    WHERE control.cancel_requested_at IS NOT NULL
                      AND (
                          job.status IN ('queued', 'awaiting_approval')
                          OR (job.status='running' AND job.lease_until < NOW())
                      )
                    FOR UPDATE OF job SKIP LOCKED
                )
                UPDATE public.aicode_jobs AS job
                SET status='cancelled',
                    result=CASE
                        WHEN job.status='running' THEN jsonb_build_object(
                            'side_effects', jsonb_build_object(
                                'reconciliation_required', true
                            ),
                            'reason', 'worker_lease_expired_during_cancellation'
                        )
                        ELSE job.result
                    END,
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                    updated_at=NOW()
                FROM cancelled
                WHERE job.id=cancelled.id
            """)
            conn.execute("""
                WITH candidate AS MATERIALIZED (
                    SELECT id, tenant_id, task_id::text AS task_id
                    FROM public.aicode_jobs
                    WHERE status='running' AND lease_until < NOW()
                      AND attempts >= max_attempts
                    ORDER BY id
                    LIMIT 1
                ),
                locked AS MATERIALIZED (
                    SELECT candidate.id
                    FROM candidate
                    CROSS JOIN LATERAL (
                        SELECT pg_advisory_xact_lock(
                            hashtextextended(candidate.tenant_id || ':' || candidate.task_id, 0)
                        )
                    ) AS task_lock
                )
                UPDATE public.aicode_jobs AS job
                SET status='failed', lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                    last_error='Lease expired after maximum attempts', updated_at=NOW()
                FROM locked, public.aicode_job_controls AS control
                WHERE job.id=locked.id
                  AND control.tenant_id=job.tenant_id
                  AND control.task_id=job.task_id
                  AND control.cancel_requested_at IS NULL
                  AND job.status='running' AND job.lease_until < NOW()
                  AND job.attempts >= job.max_attempts
            """)
            row = conn.execute("""
                WITH candidate AS (
                    SELECT job.id FROM public.aicode_jobs AS job
                    JOIN public.aicode_job_controls AS control
                      ON control.tenant_id=job.tenant_id AND control.task_id=job.task_id
                    WHERE control.cancel_requested_at IS NULL
                      AND job.attempts < job.max_attempts
                      AND ((job.status='queued' AND job.available_at <= NOW())
                        OR (job.status='running' AND job.lease_until < NOW()))
                    ORDER BY job.priority DESC, job.created_at ASC
                    FOR UPDATE OF job SKIP LOCKED
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
                  AND NOT EXISTS (
                      SELECT 1 FROM public.aicode_job_controls AS control
                      WHERE control.tenant_id=aicode_jobs.tenant_id
                        AND control.task_id=aicode_jobs.task_id
                        AND control.cancel_requested_at IS NOT NULL
                  )
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
        """Fence a bounded Git publication against lease handoff.

        The active job row is locked while the bounded push runs, so a replacement worker
        cannot reclaim the task mid-publication. Cancellation intent lives in a separate
        control row and can be committed without waiting for this publication lock. A push
        already in flight when cancellation commits is an external side effect that requires
        reconciliation; callers must use a stable task identity in publication metadata even
        when each lease receives an isolated feature branch.
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
                  AND NOT EXISTS (
                      SELECT 1 FROM public.aicode_job_controls AS control
                      WHERE control.tenant_id=aicode_jobs.tenant_id
                        AND control.task_id=aicode_jobs.task_id
                        AND control.cancel_requested_at IS NOT NULL
                  )
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
                  AND NOT EXISTS (
                      SELECT 1 FROM public.aicode_job_controls AS control
                      WHERE control.tenant_id=aicode_jobs.tenant_id
                        AND control.task_id=aicode_jobs.task_id
                        AND control.cancel_requested_at IS NOT NULL
                  )
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
                SET lease_until=CASE
                        WHEN EXISTS (
                            SELECT 1 FROM public.aicode_job_controls AS control
                            WHERE control.tenant_id=aicode_jobs.tenant_id
                              AND control.task_id=aicode_jobs.task_id
                              AND control.cancel_requested_at IS NOT NULL
                        ) THEN NOW()
                        ELSE NOW() + (180 * INTERVAL '1 second')
                    END,
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
            control = self._lock_execution_control(conn, job_id, tenant_id)
            if control is None or control[0] is not None:
                return False
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
            control = self._lock_execution_control(conn, job_id, tenant_id)
            if control is None or control[0] is not None:
                return False
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
        self,
        job_id: int,
        tenant_id: str,
        worker_id: str,
        lease_token: str,
        error: str,
        retry_delay_seconds: int = 30,
        *,
        retryable: bool = True,
    ) -> bool:
        """Fail a lease-owned task, retrying only when the caller classifies it as transient."""
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            control = self._lock_execution_control(conn, job_id, tenant_id)
            if control is None or control[0] is not None:
                return False
            cur = conn.execute("""
                UPDATE aicode_jobs
                SET status=CASE
                        WHEN NOT %s OR attempts >= max_attempts THEN 'failed'
                        ELSE 'queued'
                    END,
                    available_at=CASE
                        WHEN NOT %s OR attempts >= max_attempts THEN NOW()
                        ELSE NOW() + (%s * INTERVAL '1 second')
                    END,
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL,
                    last_error=%s, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running' AND lease_until > NOW()
            """, (
                retryable, retryable, max(0, retry_delay_seconds), error[:4000],
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
                  AND NOT EXISTS (
                      SELECT 1 FROM public.aicode_job_controls AS control
                      WHERE control.tenant_id=aicode_jobs.tenant_id
                        AND control.task_id=aicode_jobs.task_id
                        AND control.cancel_requested_at IS NOT NULL
                  )
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
                      AND NOT EXISTS (
                          SELECT 1 FROM public.aicode_job_controls AS control
                          WHERE control.tenant_id=aicode_jobs.tenant_id
                            AND control.task_id=aicode_jobs.task_id
                            AND control.cancel_requested_at IS NOT NULL
                      )
                )
            """, (job_id, tenant_id, worker_id, lease_token)).fetchone()
        return bool(row and row[0])

    def is_cancel_requested(self, task_id: str, tenant_id: str) -> bool:
        """Read the independent cancellation control row without waiting on a job publication lock."""
        if not task_id or not tenant_id:
            raise ValueError("task_id and tenant_id are required")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM public.aicode_job_controls
                    WHERE tenant_id=%s AND task_id=%s
                      AND cancel_requested_at IS NOT NULL
                )
                """,
                (tenant_id, task_id),
            ).fetchone()
        return bool(row and row[0])

    def mark_cancelled(
        self,
        job_id: int,
        tenant_id: str,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Acknowledge cancellation and persist any external side effects needing reconciliation."""
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            control = self._lock_execution_control(conn, job_id, tenant_id)
            if control is None or control[0] is None:
                return False
            cur = conn.execute(
                """
                UPDATE public.aicode_jobs
                SET status='cancelled', result=%s::jsonb,
                    lease_owner=NULL, lease_until=NULL, lease_token=NULL, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s
                  AND lease_token=%s::uuid AND status='running'
                """,
                (
                    json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                    job_id, tenant_id, worker_id, lease_token,
                ),
            )
        return cur.rowcount == 1

    def retry(
        self, task_id: str, tenant_id: str, *, audit_actor: str | None = None,
    ) -> bool:
        """Explicitly requeue a failed or approval-paused task for a tenant."""
        if not task_id or not tenant_id:
            raise ValueError("task_id and tenant_id are required")
        self._require_policy_audit_actor(audit_actor)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            canonical_task_id = self._lock_task_control(conn, task_id, tenant_id)
            row = None
            if canonical_task_id is not None:
                row = conn.execute("""
                    UPDATE aicode_jobs
                    SET status='queued', attempts=0, manual_retry_count=manual_retry_count+1,
                        available_at=NOW(), result=NULL, lease_owner=NULL, lease_until=NULL,
                        lease_token=NULL, last_error=NULL, updated_at=NOW()
                    WHERE task_id=%s AND tenant_id=%s
                      AND status IN ('awaiting_approval', 'failed')
                      AND manual_retry_count < max_manual_retries
                      AND NOT EXISTS (
                          SELECT 1 FROM public.aicode_job_controls AS control
                          WHERE control.tenant_id=aicode_jobs.tenant_id
                            AND control.task_id=aicode_jobs.task_id
                            AND control.cancel_requested_at IS NOT NULL
                      )
                    RETURNING id
                """, (canonical_task_id, tenant_id)).fetchone()
            self._record_policy_event(
                conn, tenant_id, task_id, audit_actor, "task.retry",
                "allow" if row is not None else "deny",
            )
        return row is not None

    def cancel(
        self, task_id: str, tenant_id: str, *, audit_actor: str | None = None,
    ) -> bool:
        """Persist cancellation intent independently from the row locked during publication.

        Running workers observe this control row through the heartbeat/control poll.
        The API therefore does not wait for a bounded external publish to release the job row.
        """
        if not task_id or not tenant_id:
            raise ValueError("task_id and tenant_id are required")
        self._require_policy_audit_actor(audit_actor)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            canonical_task_id = self._lock_task_control(conn, task_id, tenant_id)
            if canonical_task_id is None:
                self._record_policy_event(
                    conn, tenant_id, task_id, audit_actor, "task.cancel", "deny"
                )
                return False
            control = conn.execute(
                """
                SELECT control.cancel_requested_at, job.status
                FROM public.aicode_job_controls AS control
                JOIN public.aicode_jobs AS job
                  ON job.tenant_id=control.tenant_id AND job.task_id=control.task_id
                WHERE control.tenant_id=%s AND control.task_id=%s
                """,
                (tenant_id, canonical_task_id),
            ).fetchone()
            if control is None or control[1] in {"succeeded", "failed"}:
                self._record_policy_event(
                    conn, tenant_id, canonical_task_id, audit_actor, "task.cancel", "deny"
                )
                return False

            if control[0] is None:
                conn.execute(
                    """
                    UPDATE public.aicode_job_controls
                    SET cancel_epoch=cancel_epoch+1,
                        cancel_requested_at=NOW(),
                        requested_by=%s,
                        updated_at=NOW()
                    WHERE tenant_id=%s AND task_id=%s
                    """,
                    (audit_actor, tenant_id, canonical_task_id),
                )

            if control[1] in {"queued", "awaiting_approval"}:
                conn.execute(
                    """
                    UPDATE public.aicode_jobs
                    SET status='cancelled', lease_owner=NULL, lease_until=NULL,
                        lease_token=NULL, updated_at=NOW()
                    WHERE tenant_id=%s AND task_id=%s
                      AND status IN ('queued', 'awaiting_approval')
                    """,
                    (tenant_id, canonical_task_id),
                )

            self._record_policy_event(
                conn, tenant_id, canonical_task_id, audit_actor, "task.cancel", "allow"
            )
        return True

    def get(
        self, task_id: str, tenant_id: str, *, audit_actor: str | None = None,
    ) -> dict[str, Any] | None:
        self._require_policy_audit_actor(audit_actor)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute("""
                SELECT job.task_id::text, job.tenant_id, job.payload, job.result,
                       job.status, job.attempts, job.max_attempts, job.last_error,
                       job.created_at, job.updated_at,
                       (control.cancel_requested_at IS NOT NULL)
                FROM public.aicode_jobs AS job
                LEFT JOIN public.aicode_job_controls AS control
                  ON control.tenant_id=job.tenant_id AND control.task_id=job.task_id
                WHERE job.task_id=%s AND job.tenant_id=%s
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
            "cancel_requested": bool(row[10]),
        }
