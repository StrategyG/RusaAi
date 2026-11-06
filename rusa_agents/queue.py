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
            # The API only enqueues and reads tasks. It must not mutate or delete them.
            connection.execute("""
                DO $rusa$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = 'rusa_agent_api'
                    ) THEN
                        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE rusa_agent_tasks FROM rusa_agent_api';
                        EXECUTE 'GRANT SELECT, INSERT ON TABLE rusa_agent_tasks TO rusa_agent_api';
                    END IF;
                    IF EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = 'rusa_agent_worker'
                    ) AND EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = 'rusa_agent_api'
                    ) THEN
                        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM rusa_agent_api';
                        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM rusa_agent_api';
                    END IF;
                END
                $rusa$
            """)

    def configure_api_permissions(self) -> None:
        """Grant tenant mutations only through narrowly scoped SECURITY DEFINER functions."""
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            role_state = connection.execute("""
                SELECT
                    EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_api'),
                    EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_worker'),
                    COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname='rusa_agent_api'), false),
                    COALESCE((SELECT rolbypassrls FROM pg_roles WHERE rolname='rusa_agent_api'), false),
                    COALESCE((SELECT rolcreatedb FROM pg_roles WHERE rolname='rusa_agent_api'), false),
                    COALESCE((SELECT rolcreaterole FROM pg_roles WHERE rolname='rusa_agent_api'), false),
                    COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname='rusa_agent_worker'), false),
                    COALESCE((SELECT rolbypassrls FROM pg_roles WHERE rolname='rusa_agent_worker'), false),
                    COALESCE((SELECT rolcreatedb FROM pg_roles WHERE rolname='rusa_agent_worker'), false),
                    COALESCE((SELECT rolcreaterole FROM pg_roles WHERE rolname='rusa_agent_worker'), false),
                    current_user,
                    COALESCE(
                        (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
                        false
                    )
            """).fetchone()
            (
                api_exists, worker_exists,
                api_superuser, api_bypassrls, api_createdb, api_createrole,
                worker_superuser, worker_bypassrls, worker_createdb, worker_createrole,
                current_user, is_superuser,
            ) = role_state
            if not api_exists or not worker_exists:
                raise RuntimeError("rusa_agent_api and rusa_agent_worker roles must exist")
            if any((api_superuser, api_bypassrls, api_createdb, api_createrole)):
                raise RuntimeError("rusa_agent_api must remain a non-privileged NOBYPASSRLS role")
            if worker_superuser or not worker_bypassrls or worker_createdb or worker_createrole:
                raise RuntimeError("rusa_agent_worker must be a non-superuser BYPASSRLS service role")
            if current_user != "rusa_agent_worker" and not is_superuser:
                raise RuntimeError(
                    "API permission migration must run as rusa_agent_worker or a database superuser"
                )

            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                REVOKE ALL ON TABLES FROM rusa_agent_api
            """)
            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                REVOKE ALL ON SEQUENCES FROM rusa_agent_api
            """)
            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC
            """)
            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                REVOKE EXECUTE ON FUNCTIONS FROM rusa_agent_api
            """)
            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                IN SCHEMA public REVOKE ALL ON TABLES FROM rusa_agent_api
            """)
            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                IN SCHEMA public REVOKE ALL ON SEQUENCES FROM rusa_agent_api
            """)
            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC
            """)
            connection.execute("""
                ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker
                IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM rusa_agent_api
            """)
            connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM rusa_agent_api"
            )
            connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM rusa_agent_api"
            )
            connection.execute(
                "GRANT SELECT, INSERT ON TABLE public.rusa_agent_tasks TO rusa_agent_api"
            )

            connection.execute("""
                CREATE OR REPLACE FUNCTION public.rusa_agent_cancel_task(
                    p_tenant_id TEXT, p_task_id TEXT
                )
                RETURNS BOOLEAN
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $rusa$
                DECLARE affected_rows INTEGER;
                BEGIN
                    IF p_tenant_id IS NULL OR p_task_id IS NULL
                       OR p_tenant_id IS DISTINCT FROM current_setting('rusa.tenant_id', true)
                    THEN
                        RETURN FALSE;
                    END IF;
                    UPDATE public.rusa_agent_tasks
                    SET status='cancelled',
                        lease_owner=NULL,
                        lease_token=NULL,
                        lease_until=NULL,
                        lease_heartbeat_at=NULL,
                        last_error='cancelled by tenant',
                        updated_at=now()
                    WHERE tenant_id=p_tenant_id AND task_id=p_task_id
                      AND status IN (
                          'queued', 'retry', 'waiting_external', 'running', 'awaiting_approval'
                      );
                    GET DIAGNOSTICS affected_rows = ROW_COUNT;
                    RETURN affected_rows = 1;
                END;
                $rusa$
            """)

            connection.execute("""
                CREATE OR REPLACE FUNCTION public.rusa_agent_requeue_failed_task(
                    p_tenant_id TEXT, p_task_id TEXT
                )
                RETURNS BOOLEAN
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = pg_catalog
                AS $rusa$
                DECLARE affected_rows INTEGER;
                BEGIN
                    IF p_tenant_id IS NULL OR p_task_id IS NULL
                       OR p_tenant_id IS DISTINCT FROM current_setting('rusa.tenant_id', true)
                    THEN
                        RETURN FALSE;
                    END IF;
                    UPDATE public.rusa_agent_tasks
                    SET status='queued',
                        attempts=0,
                        result=CASE WHEN result ->> 'state' = 'failed'
                            THEN NULL ELSE result END,
                        available_at=now(),
                        lease_owner=NULL,
                        lease_token=NULL,
                        lease_until=NULL,
                        lease_heartbeat_at=NULL,
                        last_error=NULL,
                        updated_at=now()
                    WHERE tenant_id=p_tenant_id AND task_id=p_task_id
                      AND status='failed'
                      AND (result ->> 'pending_external_write') IS NULL;
                    GET DIAGNOSTICS affected_rows = ROW_COUNT;
                    RETURN affected_rows = 1;
                END;
                $rusa$
            """)

            connection.execute(
                "REVOKE ALL ON FUNCTION public.rusa_agent_cancel_task(TEXT, TEXT) FROM PUBLIC"
            )
            connection.execute(
                "REVOKE ALL ON FUNCTION public.rusa_agent_requeue_failed_task(TEXT, TEXT) FROM PUBLIC"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION public.rusa_agent_cancel_task(TEXT, TEXT) TO rusa_agent_api, rusa_agent_worker"
            )
            connection.execute(
                "GRANT EXECUTE ON FUNCTION public.rusa_agent_requeue_failed_task(TEXT, TEXT) TO rusa_agent_api, rusa_agent_worker"
            )

            api_privileges = connection.execute("""
                SELECT
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'SELECT'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'INSERT'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'UPDATE'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_tasks', 'DELETE'),
                    has_table_privilege('rusa_agent_api', 'public.rusa_agent_memory', 'SELECT'),
                    has_function_privilege(
                        'rusa_agent_api', 'public.rusa_agent_cancel_task(text,text)', 'EXECUTE'
                    ),
                    has_function_privilege(
                        'rusa_agent_api', 'public.rusa_agent_requeue_failed_task(text,text)', 'EXECUTE'
                    )
            """).fetchone()
            if api_privileges != (True, True, False, False, False, True, True):
                raise RuntimeError("tenant API database privileges do not match the least-privilege policy")

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
                       has_function_privilege(
                           current_user,
                           'public.rusa_agent_cancel_task(text,text)',
                           'EXECUTE'
                       ),
                       has_function_privilege(
                           current_user,
                           'public.rusa_agent_requeue_failed_task(text,text)',
                           'EXECUTE'
                       ),
                       CASE
                           WHEN current_user = 'rusa_agent_api' THEN
                               NOT has_table_privilege(
                                   current_user, 'public.rusa_agent_tasks', 'UPDATE'
                               )
                               AND NOT has_table_privilege(
                                   current_user, 'public.rusa_agent_tasks', 'DELETE'
                               )
                               AND NOT has_table_privilege(
                                   current_user, 'public.rusa_agent_memory', 'SELECT'
                               )
                           ELSE TRUE
                       END
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
                    SET status=CASE WHEN (result ->> 'pending_external_write') IS NOT NULL
                            THEN 'blocked' ELSE 'failed' END,
                        result=CASE WHEN (result ->> 'pending_external_write') IS NOT NULL
                            THEN jsonb_set(result, '{state}', '"blocked"') ELSE result END,
                        lease_owner=NULL,
                        lease_token=NULL,
                        lease_until=NULL,
                        last_error=CASE WHEN (result ->> 'pending_external_write') IS NOT NULL
                            THEN 'external write requires reconciliation'
                            ELSE 'maximum task attempts reached'
                        END,
                        updated_at=now()
                    WHERE attempts >= max_attempts
                      AND (
                          (status='running' AND lease_until <= clock_timestamp())
                          OR (status IN ('queued', 'retry', 'waiting_external')
                              AND available_at <= now())
                      )
                """)
                row = connection.execute("""
                    WITH candidate AS (
                        SELECT tenant_id, task_id
                        FROM rusa_agent_tasks
                        WHERE attempts < max_attempts AND (
                            (status IN ('queued', 'retry', 'waiting_external')
                             AND available_at <= now())
                            OR (status='running' AND lease_until <= clock_timestamp())
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
                        lease_until=clock_timestamp() + (%s * interval '1 second'),
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

    @staticmethod
    def _lock_task(connection, tenant_id: str, task_id: str) -> None:
        # Check the lease against the current clock after acquiring the row lock.
        connection.execute(
            "SELECT 1 FROM rusa_agent_tasks WHERE tenant_id=%s AND task_id=%s FOR UPDATE",
            (tenant_id, task_id),
        )

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
            self._lock_task(connection, tenant_id, task_id)
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET lease_until=clock_timestamp() + (%s * interval '1 second'),
                    lease_heartbeat_at=now(),
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s
                  AND lease_until > clock_timestamp()
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
                      AND lease_owner=%s AND lease_token=%s AND lease_until > clock_timestamp()
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
            self._lock_task(connection, tenant_id, task_id)
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET result=%s::jsonb, updated_at=now()
                WHERE tenant_id=%s AND task_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s
                  AND lease_until > clock_timestamp()
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
            self._lock_task(connection, tenant_id, task_id)
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
                  AND lease_until > clock_timestamp()
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
            self._lock_task(connection, tenant_id, task_id)
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET status=CASE
                        WHEN (result ->> 'pending_external_write') IS NOT NULL THEN 'blocked'
                        WHEN attempts >= max_attempts THEN 'failed'
                        ELSE 'retry'
                    END,
                    result=CASE WHEN (result ->> 'pending_external_write') IS NOT NULL
                        THEN jsonb_set(result, '{state}', '"blocked"')
                        WHEN result ->> 'state' = 'failed' THEN NULL
                        ELSE result
                    END,
                    available_at=now() + (%s * interval '1 second'),
                    lease_owner=NULL,
                    lease_token=NULL,
                    lease_until=NULL,
                    last_error=CASE WHEN (result ->> 'pending_external_write') IS NOT NULL
                        THEN 'external write requires reconciliation'
                        ELSE %s
                    END,
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s
                  AND status='running' AND lease_owner=%s AND lease_token=%s
                  AND lease_until > clock_timestamp()
            """, (delay, error[:2000], tenant_id, task_id, worker_id, lease_token))
        return cursor.rowcount == 1

    def cancel(self, tenant_id: str, task_id: str) -> bool:
        """Cancel a non-terminal task through a tenant-checked database function."""
        if not tenant_id or not task_id:
            raise ValueError("tenant_id and task_id are required")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            row = connection.execute(
                "SELECT public.rusa_agent_cancel_task(%s, %s)",
                (tenant_id, task_id),
            ).fetchone()
        return bool(row and row[0])

    def requeue_failed(self, tenant_id: str, task_id: str) -> bool:
        """Explicitly requeue a failed task through a tenant-checked database function."""
        if not tenant_id or not task_id:
            raise ValueError("tenant_id and task_id are required")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            row = connection.execute(
                "SELECT public.rusa_agent_requeue_failed_task(%s, %s)",
                (tenant_id, task_id),
            ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _workspace_record(row: tuple[Any, ...], tenant_id: str) -> dict[str, Any]:
        task_id, state, payload, result, attempts, max_attempts, last_error, created_at, updated_at = row
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(result, str):
            result = json.loads(result)
        return {
            "task_id": str(task_id),
            "tenant_id": tenant_id,
            "state": state,
            "status": state,
            "payload": payload or {},
            "result": result,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "last_error": last_error,
            "created_at": created_at.isoformat() if created_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
        }

    def list_tasks(
        self,
        tenant_id: str,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not tenant_id or not 1 <= limit <= 100 or offset < 0:
            raise ValueError("task list scope or pagination is invalid")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            rows = connection.execute(
                """
                SELECT task_id, status, payload, result, attempts, max_attempts,
                       last_error, created_at, updated_at
                FROM public.rusa_agent_tasks
                WHERE tenant_id=%s
                  AND (%s::text IS NULL OR payload->'metadata'->>'user_id'=%s)
                ORDER BY created_at DESC, task_id
                LIMIT %s OFFSET %s
                """,
                (tenant_id, user_id, user_id, limit, offset),
            ).fetchall()
        return [self._workspace_record(row, tenant_id) for row in rows]

    def stats(self, tenant_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            row = connection.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status IN ('queued', 'retry')) AS queued,
                    COUNT(*) FILTER (WHERE status='running') AS running,
                    COUNT(*) FILTER (WHERE status='succeeded') AS succeeded,
                    COUNT(*) FILTER (WHERE status='failed') AS failed,
                    COUNT(*) FILTER (WHERE status='blocked') AS blocked,
                    COUNT(*) FILTER (WHERE status='cancelled') AS cancelled,
                    COUNT(*) FILTER (WHERE status='waiting_external') AS waiting_external,
                    COALESCE(
                        EXTRACT(EPOCH FROM (now() - MIN(created_at)
                            FILTER (WHERE status IN ('queued', 'retry'))))::bigint,
                        0
                    ) AS oldest_queued_age_seconds
                FROM public.rusa_agent_tasks
                WHERE tenant_id=%s
                  AND (%s::text IS NULL OR payload->'metadata'->>'user_id'=%s)
                """,
                (tenant_id, user_id, user_id),
            ).fetchone()
        keys = ("queued", "running", "succeeded", "failed", "blocked", "cancelled", "waiting_external")
        counts = {key: int(value or 0) for key, value in zip(keys, row[:len(keys)])}
        return {"counts": counts, "oldest_queued_age_seconds": int(row[-1] or 0)}

    def list_dead_letters(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not tenant_id or not 1 <= limit <= 100:
            raise ValueError("dead-letter scope or limit is invalid")
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            rows = connection.execute(
                """
                SELECT task_id, status, payload, result, attempts, max_attempts,
                       last_error, created_at, updated_at
                FROM public.rusa_agent_tasks
                WHERE tenant_id=%s AND status='failed'
                  AND (%s::text IS NULL OR payload->'metadata'->>'user_id'=%s)
                ORDER BY updated_at DESC, task_id
                LIMIT %s
                """,
                (tenant_id, user_id, user_id, limit),
            ).fetchall()
        return [self._workspace_record(row, tenant_id) for row in rows]

    def retry_dead_letter(self, tenant_id: str, task_id: str) -> bool:
        """Compatibility name for the secure, tenant-checked failed-task requeue operation."""
        return self.requeue_failed(tenant_id, task_id)

    def get(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self._psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id,),
            )
            row = connection.execute("""
                SELECT task_id, status, payload, result, attempts, max_attempts,
                       last_error, created_at, updated_at
                FROM rusa_agent_tasks
                WHERE tenant_id=%s AND task_id=%s
            """, (tenant_id, task_id)).fetchone()
        if row is None:
            return None
        return self._workspace_record(row, tenant_id)

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
