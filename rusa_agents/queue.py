"""PostgreSQL durable queue with tenant-scoped idempotency, RLS, and worker leases."""

import json
import secrets
from typing import Any

from .contracts import CodingTask, TaskRecord, TaskState


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for a different task payload."""


class PostgresTaskQueue:
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
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(99101001)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS rusa_agent_tasks (
                    tenant_id TEXT NOT NULL, task_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', payload JSONB NOT NULL, result JSONB,
                    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 4,
                    available_at TIMESTAMPTZ NOT NULL DEFAULT now(), lease_until TIMESTAMPTZ, lease_token TEXT,
                    last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, task_id), UNIQUE (tenant_id, idempotency_key)
                )
            """)
            connection.execute("ALTER TABLE rusa_agent_tasks ADD COLUMN IF NOT EXISTS lease_token TEXT")
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_rusa_agent_queue
                ON rusa_agent_tasks(status, available_at, created_at)
            """)
            connection.execute("ALTER TABLE rusa_agent_tasks ENABLE ROW LEVEL SECURITY")
            connection.execute("ALTER TABLE rusa_agent_tasks FORCE ROW LEVEL SECURITY")
            connection.execute("""
                DO $
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_api') THEN
                        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE rusa_agent_tasks FROM rusa_agent_api';
                        EXECUTE 'GRANT SELECT, INSERT ON TABLE rusa_agent_tasks TO rusa_agent_api';
                        EXECUTE 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM rusa_agent_api';
                    END IF;
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_api')
                       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_agent_worker') THEN
                        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public REVOKE ALL ON TABLES FROM rusa_agent_api';
                        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE rusa_agent_worker IN SCHEMA public REVOKE ALL ON SEQUENCES FROM rusa_agent_api';
                    END IF;
                END $;
            """)
            connection.execute("DROP POLICY IF EXISTS rusa_agent_tenant_isolation ON rusa_agent_tasks")
            connection.execute("""
                CREATE POLICY rusa_agent_tenant_isolation ON rusa_agent_tasks
                USING (tenant_id = current_setting('rusa.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true))
            """)

    def check_ready(self) -> bool:
        with self._psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT to_regclass('public.rusa_agent_tasks')"
            ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _normalized_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized.pop("task_id", None)
        normalized.pop("created_at", None)
        return normalized

    def enqueue(self, task: CodingTask, max_attempts: int = 4) -> str:
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        key = task.idempotency_key or task.task_id
        payload = task.to_dict()
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (task.tenant_id,))
            inserted = connection.execute("""
                INSERT INTO rusa_agent_tasks
                    (tenant_id, task_id, idempotency_key, payload, max_attempts)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING task_id
            """, (
                task.tenant_id, task.task_id, key,
                json.dumps(payload, ensure_ascii=False, sort_keys=True), max_attempts,
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

    def claim(self, lease_seconds: int = 300) -> dict[str, Any] | None:
        if not 10 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        lease_token = secrets.token_urlsafe(32)
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("""
                UPDATE rusa_agent_tasks
                SET status='failed', last_error='worker lease expired after maximum attempts',
                    lease_until=NULL, lease_token=NULL, updated_at=now()
                WHERE status='running' AND lease_until < now() AND attempts >= max_attempts
            """)
            row = connection.execute("""
                WITH candidate AS (
                    SELECT tenant_id, task_id
                    FROM rusa_agent_tasks
                    WHERE (
                        status IN ('queued', 'retry', 'waiting_external')
                        AND available_at <= now()
                    ) OR (
                        status='running' AND lease_until < now() AND attempts < max_attempts
                    )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE rusa_agent_tasks AS task
                SET status='running', attempts=task.attempts + 1,
                    lease_until=now() + (%s * interval '1 second'),
                    lease_token=%s, updated_at=now()
                FROM candidate
                WHERE task.tenant_id=candidate.tenant_id AND task.task_id=candidate.task_id
                RETURNING task.tenant_id, task.task_id, task.payload, task.result,
                          task.attempts, task.lease_token
            """, (lease_seconds, lease_token)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        result = json.loads(row[3]) if isinstance(row[3], str) else row[3]
        return {
            "tenant_id": row[0], "task_id": row[1], "payload": payload,
            "result": result, "attempts": row[4], "lease_token": row[5],
        }

    def renew_lease(
        self, tenant_id: str, task_id: str, lease_token: str, lease_seconds: int = 300,
    ) -> bool:
        if not 10 <= lease_seconds <= 3600 or not lease_token:
            raise ValueError("a valid lease token and lease duration are required")
        with self._psycopg.connect(self.dsn) as connection:
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET lease_until=now() + (%s * interval '1 second'), updated_at=now()
                WHERE tenant_id=%s AND task_id=%s AND status='running'
                  AND lease_token=%s AND lease_until > now()
            """, (lease_seconds, tenant_id, task_id, lease_token))
        return cursor.rowcount == 1

    def acknowledge(
        self, tenant_id: str, task_id: str, record: TaskRecord, lease_token: str,
    ) -> None:
        queue_status = (
            "waiting_external" if record.state == TaskState.WAITING_EXTERNAL else record.state.value
        )
        delay = 30 if record.state == TaskState.WAITING_EXTERNAL else 0
        with self._psycopg.connect(self.dsn) as connection:
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET status=%s, result=%s::jsonb, lease_until=NULL, lease_token=NULL,
                    available_at=now() + (%s * interval '1 second'),
                    updated_at=now(), last_error=NULL
                WHERE tenant_id=%s AND task_id=%s AND status='running'
                  AND lease_token=%s AND lease_until > now()
            """, (queue_status, json.dumps(record.to_dict()), delay, tenant_id, task_id, lease_token))
            if cursor.rowcount != 1:
                raise RuntimeError("task lease was lost before acknowledgement")

    def retry(
        self, tenant_id: str, task_id: str, error: str, lease_token: str, *,
        delay_seconds: int = 10,
    ) -> None:
        with self._psycopg.connect(self.dsn) as connection:
            cursor = connection.execute("""
                UPDATE rusa_agent_tasks
                SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retry' END,
                    result=NULL,
                    available_at=now() + (%s * interval '1 second'),
                    lease_until=NULL, lease_token=NULL, last_error=%s, updated_at=now()
                WHERE tenant_id=%s AND task_id=%s AND status='running'
                  AND lease_token=%s AND lease_until > now()
            """, (max(1, delay_seconds), error[:2000], tenant_id, task_id, lease_token))
            if cursor.rowcount != 1:
                raise RuntimeError("task lease was lost before retry")

    def get(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            row = connection.execute("""
                SELECT status, result, attempts, last_error, created_at, updated_at
                FROM rusa_agent_tasks
                WHERE tenant_id=%s AND task_id=%s
            """, (tenant_id, task_id)).fetchone()
        if row is None:
            return None
        result = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        return {
            "status": row[0], "result": result, "attempts": row[2], "last_error": row[3],
            "created_at": row[4].isoformat() if row[4] else None,
            "updated_at": row[5].isoformat() if row[5] else None,
        }

    def purge_terminal(self, retention_days: int = 30) -> int:
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        with self._psycopg.connect(self.dsn) as connection:
            cursor = connection.execute("""
                DELETE FROM rusa_agent_tasks
                WHERE status IN ('succeeded', 'blocked', 'failed', 'cancelled')
                  AND updated_at < now() - (%s * interval '1 day')
            """, (retention_days,))
            return cursor.rowcount
