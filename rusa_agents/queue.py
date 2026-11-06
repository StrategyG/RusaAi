"""PostgreSQL durable task queue with RLS, idempotency, checkpoints, and fenced leases."""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from .contracts import CodingTask, TaskRecord, TaskState


class IdempotencyConflictError(ValueError):
    """Raised when a tenant reuses an idempotency key for a different task."""


class PostgresTaskQueue:
    MIN_LEASE_SECONDS = 10
    MAX_LEASE_SECONDS = 3600

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("install requirements-agent.txt to use the durable queue") from exc
        self._psycopg = psycopg
        self.dsn = dsn

    def initialize(self) -> None:
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(99101001)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS rusa_agent_tasks (
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    payload JSONB NOT NULL,
                    result JSONB,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL DEFAULT 4 CHECK (max_attempts BETWEEN 1 AND 10),
                    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_until TIMESTAMPTZ,
                    lease_heartbeat_at TIMESTAMPTZ,
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, task_id),
                    UNIQUE (tenant_id, idempotency_key)
                )
            """)
            connection.execute(
                "ALTER TABLE rusa_agent_tasks ADD COLUMN IF NOT EXISTS lease_owner TEXT"
            )
            connection.execute(
                "ALTER TABLE rusa_agent_tasks ADD COLUMN IF NOT EXISTS lease_token TEXT"
            )
            connection.execute(
                "ALTER TABLE rusa_agent_tasks ADD COLUMN IF NOT EXISTS lease_heartbeat_at TIMESTAMPTZ"
            )
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_rusa_agent_queue
                ON rusa_agent_tasks(status, available_at, created_at)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_rusa_agent_lease
                ON rusa_agent_tasks(status, lease_until)
            """)
            connection.execute("ALTER TABLE rusa_agent_tasks ENABLE ROW LEVEL SECURITY")
            connection.execute("ALTER TABLE rusa_agent_tasks FORCE ROW LEVEL SECURITY")
            connection.execute("DROP POLICY IF EXISTS rusa_agent_tenant_isolation ON rusa_agent_tasks")
            connection.execute("""
                CREATE POLICY rusa_agent_tenant_isolation ON rusa_agent_tasks
                USING (tenant_id = current_setting('rusa.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true))
            """)


    def configure_api_permissions(self) -> None:
        """Apply least-privilege grants for the tenant-facing API role.

        The migration identity must own the queue table (or be a superuser).
        Existing grants and worker default privileges are revoked so future
        worker-created tables do not become visible to the API automatically.
        """
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            api_role_exists = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_api')"
            ).fetchone()[0]
            if not api_role_exists:
                raise RuntimeError("rusa_agent_api role is missing")

            role_state = connection.execute(
                """
                SELECT current_user,
                       COALESCE(
                           (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
                           false
                       ),
                       EXISTS (
                           SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_worker'
                       )
                """
            ).fetchone()
            current_user, is_superuser, worker_role_exists = role_state
            if worker_role_exists and (
                current_user == "rusa_agent_worker" or is_superuser
            ):
                connection.execute(
                    """
                    ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                    IN SCHEMA public REVOKE ALL ON TABLES FROM rusa_agent_api
                    """
                )
                connection.execute(
                    """
                    ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                    IN SCHEMA public REVOKE ALL ON SEQUENCES FROM rusa_agent_api
                    """
                )

            connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM rusa_agent_api"
            )
            connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM rusa_agent_api"
            )
            connection.execute(
                "GRANT SELECT, INSERT, UPDATE ON TABLE public.rusa_agent_tasks TO rusa_agent_api"
            )


    def check_ready(self) -> bool:
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            row = connection.execute("""
                SELECT c.relrowsecurity, c.relforcerowsecurity,
                       EXISTS (
                           SELECT 1 FROM pg_policies
                           WHERE schemaname='public'
                             AND tablename='rusa_agent_tasks'
                             AND policyname='rusa_agent_tenant_isolation'
                       ),
                       EXISTS (
                           SELECT 1 FROM information_schema.columns
                           WHERE table_schema='public'
                             AND table_name='rusa_agent_tasks'
                             AND column_name='lease_token'
                       ),
                       has_table_privilege(
                           current_user, 'public.rusa_agent_tasks', 'SELECT'
                       ),
                       has_table_privilege(
                           current_user, 'public.rusa_agent_tasks', 'INSERT'
                       ),
                       has_table_privilege(
                           current_user, 'public.rusa_agent_tasks', 'UPDATE'
                       )
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname='rusa_agent_tasks'
            """).fetchone()
        return bool(row and all(row))

    @staticmethod
    def _validate_worker_id(worker_id: str) -> None:
        if not isinstance(worker_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", worker_id,
        ):
            raise ValueError("worker_id must be a valid identifier of at most 128 characters")

    @staticmethod
    def _validate_lease(lease_seconds: int) -> None:
        if (
            type(lease_seconds) is not int
            or not PostgresTaskQueue.MIN_LEASE_SECONDS
            <= lease_seconds
            <= PostgresTaskQueue.MAX_LEASE_SECONDS
        ):
            raise ValueError(
                "lease_seconds must be an integer between "
                f"{PostgresTaskQueue.MIN_LEASE_SECONDS} and {PostgresTaskQueue.MAX_LEASE_SECONDS}"
            )

    @staticmethod
    def _validate_lease_token(lease_token: str) -> None:
        if not isinstance(lease_token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32,128}", lease_token,
        ):
            raise ValueError("lease_token is invalid")

    @staticmethod
    def _normalized_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized.pop("created_at", None)
        normalized.pop("task_id", None)
        return normalized

    def enqueue(self, task: CodingTask, max_attempts: int = 4) -> str:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be an integer between 1 and 10")
        key = task.idempotency_key or task.task_id
        payload = task.to_dict()
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (task.tenant_id,),
            )
            inserted = connection.execute("""
                INSERT INTO rusa_agent_tasks
                    (tenant_id, task_id, idempotency_key, payload, max_attempts)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING task_id
            """, (
                task.tenant_id,
                task.task_id,
                key,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                max_attempts,
            )).fetchone()
            if inserted is not None:
                return str(inserted[0])
            existing = connection.execute("""
                SELECT task_id, payload FROM rusa_agent_tasks
                WHERE tenant_id=%s AND idempotency_key=%s
            """, (task.tenant_id, key)).fetchone()
            if existing is None:
                raise RuntimeError("idempotency conflict could not be resolved")
            stored_payload = existing[1]
            if isinstance(stored_payload, str):
                stored_payload = json.loads(stored_payload)
            if self._normalized_payload(stored_payload) != self._normalized_payload(payload):
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different task payload"
                )
            return str(existing[0])

    def claim(
        self,
        worker_id: str = "worker-default",
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        self._validate_worker_id(worker_id)
        self._validate_lease(lease_seconds)
        lease_token = secrets.token_urlsafe(32)
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            with connection.transaction():
                connection.execute("""
                    UPDATE rusa_agent_tasks
                    SET status='failed',
                        lease_owner=NULL,
                        lease_token=NULL,
                        lease_until=NULL,
                        last_error=COALESCE(
                            last_error, 'worker lease expired after maximum attempts'
                        ),
                        updated_at=now()
                    WHERE status='running'
                      AND lease_until < now()
                      AND attempts >= max_attempts
                """)
                row = connection.execute("""
                    WITH candidate AS (
                        SELECT tenant_id, task_id
                        FROM rusa_agent_tasks
                        WHERE (
                            status IN ('queued', 'retry', 'waiting_external')
                            AND available_at <= now()
                        ) OR (
                            status='running'
                            AND lease_until < now()
                            AND attempts < max_attempts
                        )
                        ORDER BY available_at, created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE rusa_agent_tasks AS task
                    SET status='running',
                        attempts=task.attempts + 1,
                        lease_owner=%s,
                        lease_token=%s,
                        lease_until=now() + (%s * interval '1 second'),
                        lease_heartbeat_at=now(),
                        updated_at=now()
                    FROM candidate
                    WHERE task.tenant_id=candidate.tenant_id
                      AND task.task_id=candidate.task_id
                    RETURNING task.tenant_id, task.task_id, task.payload, task.result,
                              task.attempts, task.max_attempts, task.lease_owner,
                              task.lease_token, task.lease_until
                """, (worker_id, lease_token, lease_seconds)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        result = json.loads(row[3]) if isinstance(row[3], str) else row[3]
        return {
            "tenant_id": row[0],
            "task_id": row[1],
            "payload": payload,
            "result": result,
            "attempts": row[4],
            "max_attempts": row[5],
            "lease_owner": row[6],
            "lease_token": row[7],
            "lease_until": row[8].isoformat() if row[8] else None,
        }

    def heartbeat(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 300,
    ) -> bool:
        self._validate_worker_id(worker_id)
        self._validate_lease_token(lease_token)
        self._validate_lease(lease_seconds)
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET lease_until=now() + (%s * interval '1 second'),
                    lease_heartbeat_at=now(),
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s
                  AND lease_until > now()
            """, (lease_seconds, tenant_id, task_id, worker_id, lease_token))
        return cursor.rowcount == 1

    def owns_lease(
        self, tenant_id: str, task_id: str, worker_id: str, lease_token: str,
    ) -> bool:
        self._validate_worker_id(worker_id)
        self._validate_lease_token(lease_token)
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM rusa_agent_tasks
                    WHERE tenant_id=%s AND task_id=%s AND status='running'
                      AND lease_owner=%s AND lease_token=%s AND lease_until > now()
                )
                """,
                (tenant_id, task_id, worker_id, lease_token),
            ).fetchone()
        return bool(row and row[0])

    def checkpoint(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_token: str,
        record: TaskRecord,
    ) -> bool:
        self._validate_worker_id(worker_id)
        self._validate_lease_token(lease_token)
        if record.task.tenant_id != tenant_id or record.task.task_id != task_id:
            raise ValueError("checkpoint identity does not match its queue envelope")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET result=%s::jsonb, updated_at=now()
                WHERE tenant_id=%s AND task_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s
                  AND lease_until > now()
            """, (
                json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True),
                tenant_id,
                task_id,
                worker_id,
                lease_token,
            ))
        return cursor.rowcount == 1

    def acknowledge(
        self,
        tenant_id: str,
        task_id: str,
        record: TaskRecord,
        worker_id: str,
        lease_token: str,
    ) -> None:
        self._validate_worker_id(worker_id)
        self._validate_lease_token(lease_token)
        if record.task.tenant_id != tenant_id or record.task.task_id != task_id:
            raise ValueError("acknowledgement identity does not match its queue envelope")
        queue_status = (
            "waiting_external"
            if record.state == TaskState.WAITING_EXTERNAL
            else record.state.value
        )
        delay = 30 if record.state == TaskState.WAITING_EXTERNAL else 0
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET status=%s,
                    result=%s::jsonb,
                    lease_owner=NULL,
                    lease_token=NULL,
                    lease_until=NULL,
                    available_at=now() + (%s * interval '1 second'),
                    updated_at=now(),
                    last_error=NULL
                WHERE tenant_id=%s AND task_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s
                  AND lease_until > now()
            """, (
                queue_status,
                json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True),
                delay,
                tenant_id,
                task_id,
                worker_id,
                lease_token,
            ))
        if cursor.rowcount != 1:
            raise RuntimeError("task lease was lost before acknowledgement")

    def retry(
        self,
        tenant_id: str,
        task_id: str,
        error: str,
        worker_id: str,
        lease_token: str,
        *,
        delay_seconds: int = 10,
    ) -> bool:
        self._validate_worker_id(worker_id)
        self._validate_lease_token(lease_token)
        if not isinstance(error, str):
            raise ValueError("retry error must be a string")
        if type(delay_seconds) is not int:
            raise ValueError("delay_seconds must be an integer")
        delay = min(300, max(1, delay_seconds))
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET status=CASE
                        WHEN attempts >= max_attempts THEN 'failed'
                        ELSE 'retry'
                    END,
                    result=NULL,
                    available_at=now() + (%s * interval '1 second'),
                    lease_owner=NULL,
                    lease_token=NULL,
                    lease_until=NULL,
                    last_error=%s,
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s
                  AND lease_until > now()
            """, (delay, error[:2000], tenant_id, task_id, worker_id, lease_token))
        return cursor.rowcount == 1

    def cancel(self, tenant_id: str, task_id: str) -> bool:
        """Cancel a task that has not reached a terminal state, revoking its lease."""
        if not tenant_id or not task_id:
            raise ValueError("tenant_id and task_id are required")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET status='cancelled',
                    lease_owner=NULL,
                    lease_token=NULL,
                    lease_until=NULL,
                    lease_heartbeat_at=NULL,
                    last_error='cancelled by tenant',
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s
                  AND status IN (
                      'queued', 'retry', 'waiting_external', 'running', 'awaiting_approval'
                  )
            """, (tenant_id, task_id))
        return cursor.rowcount == 1

    def requeue_failed(self, tenant_id: str, task_id: str) -> bool:
        """Explicitly requeue a failed task for its owning tenant."""
        if not tenant_id or not task_id:
            raise ValueError("tenant_id and task_id are required")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET status='queued',
                    attempts=0,
                    result=NULL,
                    available_at=now(),
                    lease_owner=NULL,
                    lease_token=NULL,
                    lease_until=NULL,
                    lease_heartbeat_at=NULL,
                    last_error=NULL,
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s AND status='failed'
            """, (tenant_id, task_id))
        return cursor.rowcount == 1

    def get(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            row = connection.execute("""
                SELECT status, result, attempts, last_error, created_at, updated_at
                FROM rusa_agent_tasks
                WHERE tenant_id=%s AND task_id=%s
            """, (tenant_id, task_id)).fetchone()
        if row is None:
            return None
        result = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        return {
            "status": row[0],
            "result": result,
            "attempts": row[2],
            "last_error": row[3],
            "created_at": row[4].isoformat() if row[4] else None,
            "updated_at": row[5].isoformat() if row[5] else None,
        }

    def purge_terminal(self, retention_days: int = 30) -> int:
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            cursor = connection.execute("""
                DELETE FROM rusa_agent_tasks
                WHERE status IN ('succeeded', 'blocked', 'failed', 'cancelled')
                  AND updated_at < now() - (%s * interval '1 day')
            """, (retention_days,))
        return cursor.rowcount
