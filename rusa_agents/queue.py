"""PostgreSQL durable queue with idempotency, leases, retries, and dead letters."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


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
            db.execute("SELECT pg_advisory_xact_lock(%s)", (812450731,))
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
            # Apply compatibility DDL only when the existing schema needs it.
            heartbeat_column = db.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='rusa_agent_jobs'
                  AND column_name='lease_heartbeat_at'
                """
            ).fetchone()
            if heartbeat_column is None:
                db.execute(
                    "ALTER TABLE rusa_agent_jobs "
                    "ADD COLUMN lease_heartbeat_at TIMESTAMPTZ"
                )

            state_constraint = db.execute(
                """
                SELECT pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conrelid='rusa_agent_jobs'::regclass
                  AND conname='rusa_agent_jobs_state_check'
                """
            ).fetchone()
            if state_constraint and "'cancelled'" not in state_constraint["definition"]:
                db.execute(
                    "ALTER TABLE rusa_agent_jobs "
                    "DROP CONSTRAINT rusa_agent_jobs_state_check"
                )
                state_constraint = None
            if state_constraint is None:
                db.execute(
                    """
                    ALTER TABLE rusa_agent_jobs
                    ADD CONSTRAINT rusa_agent_jobs_state_check
                    CHECK (state IN (
                        'queued', 'running', 'succeeded', 'failed',
                        'dead_letter', 'cancelled'
                    ))
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
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        if not isinstance(tenant_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", tenant_id
        ):
            raise ValueError("tenant_id must contain 1 to 128 safe characters")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
            raise ValueError("idempotency_key must contain 8 to 128 characters")
        if not isinstance(payload, dict):
            raise ValueError("task payload must be a JSON object")
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        try:
            normalized_task_id = str(UUID(task_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("task_id must be a UUID") from exc
        payload_task_id = payload.get("task_id")
        if payload_task_id is None:
            raise ValueError("task payload must include task_id")
        try:
            normalized_payload_task_id = str(UUID(payload_task_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("payload task_id must be a UUID") from exc
        if normalized_payload_task_id != normalized_task_id:
            raise ValueError("payload task_id must match the queue task_id")
        payload_tenant_id = payload.get("tenant_id")
        if payload_tenant_id != tenant_id:
            raise ValueError("payload tenant_id must match the queue tenant_id")

        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as db:
            row = db.execute(
                """
                INSERT INTO rusa_agent_jobs(
                    task_id, tenant_id, idempotency_key, payload, max_attempts
                )
                VALUES (%s::uuid, %s, %s, %s::jsonb, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING task_id, state, payload
                """,
                (task_id, tenant_id, idempotency_key, serialized, max_attempts),
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

    def cancel(self, tenant_id: str, task_id: str) -> bool:
        """Cancel queued/running work and fence off any current worker lease."""
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE rusa_agent_jobs
                SET state='cancelled',
                    lease_owner=NULL,
                    lease_until=NULL,
                    last_error='cancelled by authenticated tenant',
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s::uuid
                    AND state IN ('queued', 'running')
                """,
                (tenant_id, task_id),
            )
            if cursor.rowcount == 1:
                return True
            row = db.execute(
                "SELECT state FROM rusa_agent_jobs WHERE tenant_id=%s AND task_id=%s::uuid",
                (tenant_id, task_id),
            ).fetchone()
        return bool(row and row["state"] == "cancelled")

    def list_dead_letters(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tenant_id):
            raise ValueError("tenant_id must contain 1 to 128 safe characters")
        if not 1 <= limit <= 100:
            raise ValueError("dead-letter limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT task_id, state, attempts, max_attempts, last_error,
                       created_at, updated_at
                FROM rusa_agent_jobs
                WHERE tenant_id=%s AND state='dead_letter'
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def retry_dead_letter(self, tenant_id: str, task_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE rusa_agent_jobs
                SET state='queued',
                    attempts=0,
                    available_at=now(),
                    lease_owner=NULL,
                    lease_until=NULL,
                    result=NULL,
                    last_error=NULL,
                    updated_at=now()
                WHERE tenant_id=%s AND task_id=%s::uuid
                    AND state='dead_letter'
                """,
                (tenant_id, task_id),
            )
        return cursor.rowcount == 1

    def stats(self, tenant_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tenant_id):
            raise ValueError("tenant_id must contain 1 to 128 safe characters")
        counts = {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "dead_letter": 0,
            "cancelled": 0,
        }
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT state, COUNT(*)::int AS task_count
                FROM rusa_agent_jobs
                WHERE tenant_id=%s
                GROUP BY state
                """,
                (tenant_id,),
            ).fetchall()
            age_row = db.execute(
                """
                SELECT COALESCE(
                    EXTRACT(EPOCH FROM now() - MIN(created_at)),
                    0
                )::float AS oldest_queued_age_seconds
                FROM rusa_agent_jobs
                WHERE tenant_id=%s AND state='queued'
                """,
                (tenant_id,),
            ).fetchone()
        for row in rows:
            counts[row["state"]] = row["task_count"]
        return {
            "tenant_id": tenant_id,
            "counts": counts,
            "oldest_queued_age_seconds": max(
                0.0,
                age_row["oldest_queued_age_seconds"] if age_row else 0.0,
            ),
        }

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
