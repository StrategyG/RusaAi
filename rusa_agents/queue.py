"""PostgreSQL-backed durable task queue using SKIP LOCKED leases."""

from __future__ import annotations

import json
from typing import Any


class PostgresTaskQueue:
    def __init__(self, dsn: str) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
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
                    state TEXT NOT NULL DEFAULT 'queued'
                        CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    lease_owner TEXT,
                    lease_until TIMESTAMPTZ,
                    result JSONB,
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (tenant_id, idempotency_key)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rusa_agent_jobs_claim "
                "ON rusa_agent_jobs(state, available_at, created_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rusa_agent_jobs_tenant "
                "ON rusa_agent_jobs(tenant_id, created_at DESC)"
            )

    @staticmethod
    def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if isinstance(result.get("payload"), str):
            result["payload"] = json.loads(result["payload"])
        if isinstance(result.get("result"), str):
            result["result"] = json.loads(result["result"])
        result["task_id"] = str(result["task_id"])
        return result

    def enqueue(
        self,
        tenant_id: str,
        task_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                """
                INSERT INTO rusa_agent_jobs(task_id, tenant_id, idempotency_key, payload)
                VALUES (%s::uuid, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, idempotency_key)
                DO UPDATE SET updated_at=now()
                WHERE rusa_agent_jobs.payload = EXCLUDED.payload
                RETURNING task_id, state
                """,
                (task_id, tenant_id, idempotency_key, json.dumps(payload, ensure_ascii=False)),
            ).fetchone()
        if row is None:
            raise ValueError("idempotency key was already used with a different task payload")
        return {"task_id": str(row["task_id"]), "state": row["state"]}

    def heartbeat(self, tenant_id: str, task_id: str, worker_id: str, lease_seconds: int = 1800) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE rusa_agent_jobs
                SET lease_until=now() + make_interval(secs => %s), updated_at=now()
                WHERE tenant_id=%s AND task_id=%s::uuid
                    AND state='running' AND lease_owner=%s
                """,
                (lease_seconds, tenant_id, task_id, worker_id),
            )
        return cursor.rowcount == 1

    def claim(self, worker_id: str, lease_seconds: int = 1800) -> dict[str, Any] | None:
        with self._connect() as db:
            with db.transaction():
                row = db.execute(
                    """
                    SELECT task_id, tenant_id, payload, attempts, max_attempts
                    FROM rusa_agent_jobs
                    WHERE (
                        (state='queued' AND available_at <= now())
                        OR (state='running' AND lease_until < now())
                    )
                    AND attempts < max_attempts
                    ORDER BY created_at
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
                        updated_at=now()
                    WHERE task_id=%s
                    RETURNING task_id, tenant_id, payload, attempts, max_attempts
                    """,
                    (worker_id, lease_seconds, row["task_id"]),
                ).fetchone()
        return self._decode(claimed)

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
                SET state=CASE WHEN %s AND attempts < max_attempts
                               THEN 'queued' ELSE 'failed' END,
                    available_at=now() + (LEAST(300, GREATEST(5, power(2, attempts)::int * 5))
                                           * interval '1 second'),
                    lease_owner=NULL, lease_until=NULL,
                    last_error=%s, updated_at=now()
                WHERE tenant_id=%s AND task_id=%s::uuid
                    AND state='running' AND lease_owner=%s
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
