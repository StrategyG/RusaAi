"""PostgreSQL durable queue with idempotency, leases, retries, and dead letters."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


class PostgresTaskQueue:
    MIN_LEASE_SECONDS = 30
    MAX_LEASE_SECONDS = 86400

    def __init__(self, dsn: str) -> None:
        parsed = urlsplit(dsn)
        if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL connection URL")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is required for the PostgreSQL task queue") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        self.ensure_schema()

    def _connect(self):
        return self._psycopg.connect(
            self.dsn,
            connect_timeout=5,
            row_factory=self._dict_row,
        )

    def ensure_schema(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rusa_agent_jobs (
                    task_id UUID PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    max_attempts INTEGER NOT NULL DEFAULT 3
                        CHECK (max_attempts BETWEEN 1 AND 20),
                    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    lease_owner TEXT,
                    lease_until TIMESTAMPTZ,
                    lease_heartbeat_at TIMESTAMPTZ,
                    result JSONB,
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (tenant_id, idempotency_key)
                )
                """
            )
            # Upgrade installations created by earlier queue versions.
            db.execute(
                "ALTER TABLE rusa_agent_jobs ADD COLUMN IF NOT EXISTS "
                "lease_heartbeat_at TIMESTAMPTZ"
            )
            db.execute(
                "ALTER TABLE rusa_agent_jobs DROP CONSTRAINT IF EXISTS "
                "rusa_agent_jobs_state_check"
            )
            db.execute(
                """
                ALTER TABLE rusa_agent_jobs
                ADD CONSTRAINT rusa_agent_jobs_state_check
                CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'dead_letter'))
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rusa_agent_jobs_claim "
                "ON rusa_agent_jobs(state, available_at, created_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rusa_agent_jobs_lease "
                "ON rusa_agent_jobs(state, lease_until)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rusa_agent_jobs_tenant "
                "ON rusa_agent_jobs(tenant_id, created_at DESC)"
            )

    @staticmethod
    def _validate_lease(lease_seconds: int) -> None:
        if not PostgresTaskQueue.MIN_LEASE_SECONDS <= lease_seconds <= PostgresTaskQueue.MAX_LEASE_SECONDS:
            raise ValueError(
                "lease_seconds must be between "
                f"{PostgresTaskQueue.MIN_LEASE_SECONDS} and "
                f"{PostgresTaskQueue.MAX_LEASE_SECONDS}"
            )

    @staticmethod
    def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("payload", "result"):
            if isinstance(result.get(key), str):
                result[key] = json.loads(result[key])
        if "task_id" in result:
            result["task_id"] = str(result["task_id"])
        return result

    def enqueue(
        self,
        tenant_id: str,
        task_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not tenant_id or len(tenant_id) > 128:
            raise ValueError("tenant_id must contain 1 to 128 characters")
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("idempotency_key must contain 1 to 128 characters")
        if not isinstance(payload, dict):
            raise ValueError("task payload must be a JSON object")

        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as db:
            row = db.execute(
                """
                INSERT INTO rusa_agent_jobs(task_id, tenant_id, idempotency_key, payload)
                VALUES (%s::uuid, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING task_id, state, payload
                """,
                (task_id, tenant_id, idempotency_key, serialized),
            ).fetchone()
            if row is None:
                row = db.execute(
                    """
                    SELECT task_id, state, payload
                    FROM rusa_agent_jobs
                    WHERE tenant_id=%s AND idempotency_key=%s
                    """,
                    (tenant_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise RuntimeError("idempotency conflict could not be resolved")
                if row["payload"] != payload:
                    raise ValueError(
                        "idempotency key was already used with a different task payload"
                    )
        return {"task_id": str(row["task_id"]), "state": row["state"]}

    def claim(self, worker_id: str, lease_seconds: int = 1800) -> dict[str, Any] | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        self._validate_lease(lease_seconds)
        with self._connect() as db:
            with db.transaction():
                # Expired jobs that exhausted their retry budget must not remain running forever.
                db.execute(
                    """
                    UPDATE rusa_agent_jobs
                    SET state='dead_letter',
                        lease_owner=NULL,
                        lease_until=NULL,
                        last_error=COALESCE(last_error, 'worker lease expired after max attempts'),
                        updated_at=now()
                    WHERE state='running'
                      AND lease_until <= now()
                      AND attempts >= max_attempts
                    """
                )
                row = db.execute(
                    """
                    SELECT task_id
                    FROM rusa_agent_jobs
                    WHERE (
                        (state='queued' AND available_at <= now())
                        OR (state='running' AND lease_until <= now()
                            AND attempts < max_attempts)
                    )
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                claimed = db.execute(
                    """
                    UPDATE rusa_agent_jobs
                    SET state='running',
                        attempts=attempts+1,
                        lease_owner=%s,
                        lease_until=now() + (%s * interval '1 second'),
                        lease_heartbeat_at=now(),
                        updated_at=now()
                    WHERE task_id=%s
                    RETURNING task_id, tenant_id, payload, attempts, max_attempts, lease_until
                    """,
                    (worker_id, lease_seconds, row["task_id"]),
                ).fetchone()
        return self._decode(claimed)

    def heartbeat(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_seconds: int = 1800,
    ) -> bool:
        self._validate_lease(lease_seconds)
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE rusa_agent_jobs
                SET lease_until=now() + (%s * interval '1 second'),
                    lease_heartbeat_at=now(),
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s::uuid
                  AND state='running' AND lease_owner=%s
                  AND lease_until > now()
                """,
                (lease_seconds, tenant_id, task_id, worker_id),
            )
        return cursor.rowcount == 1

    def complete(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        result: dict[str, Any],
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE rusa_agent_jobs
                SET state='succeeded', result=%s::jsonb, lease_owner=NULL,
                    lease_until=NULL, last_error=NULL, updated_at=now()
                WHERE tenant_id=%s AND task_id=%s::uuid
                    AND state='running' AND lease_owner=%s
                    AND lease_until > now()
                """,
                (json.dumps(result, ensure_ascii=False), tenant_id, task_id, worker_id),
            )
        return cursor.rowcount == 1

    def fail(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        error: str,
        retryable: bool = True,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE rusa_agent_jobs
                SET state=CASE
                        WHEN %s AND attempts < max_attempts THEN 'queued'
                        WHEN attempts >= max_attempts THEN 'dead_letter'
                        ELSE 'failed'
                    END,
                    available_at=now() + (
                        LEAST(300, GREATEST(5, power(2, attempts)::int * 5))
                        * interval '1 second'
                    ),
                    lease_owner=NULL,
                    lease_until=NULL,
                    last_error=%s,
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s::uuid
                    AND state='running' AND lease_owner=%s
                    AND lease_until > now()
                """,
                (retryable, error[:4000], tenant_id, task_id, worker_id),
            )
        return cursor.rowcount == 1

    def get(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT task_id, tenant_id, state, attempts, max_attempts,
                       result, last_error, created_at, updated_at
                FROM rusa_agent_jobs
                WHERE tenant_id=%s AND task_id=%s::uuid
                """,
                (tenant_id, task_id),
            ).fetchone()
        return self._decode(row)
