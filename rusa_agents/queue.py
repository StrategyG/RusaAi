"""PostgreSQL-backed durable queue with leases, retries, and tenant scoping."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Job:
    id: int
    task_id: str
    tenant_id: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str


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

    def initialize(self) -> None:
        with self._connect() as conn:
            if os.getenv("ENVIRONMENT", "development").lower() == "production":
                exists = conn.execute("SELECT to_regclass('public.aicode_jobs')").fetchone()
                if not exists or exists[0] is None:
                    raise RuntimeError("Production database schema is missing; apply migrations/001_aicode_core.sql")
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
                    last_error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (tenant_id, task_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_aicode_jobs_claim
                ON aicode_jobs(status, available_at, priority DESC, created_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_aicode_jobs_tenant
                ON aicode_jobs(tenant_id, created_at DESC)
            """)

    def enqueue(self, task_id: str, tenant_id: str, payload: dict[str, Any],
                max_attempts: int = 3, priority: int = 0) -> bool:
        if not tenant_id or not task_id or max_attempts < 1:
            raise ValueError("tenant_id, task_id, and positive max_attempts are required")
        with self._connect() as conn:
            row = conn.execute("""
                INSERT INTO aicode_jobs(task_id, tenant_id, payload, max_attempts, priority)
                VALUES (%s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (tenant_id, task_id) DO NOTHING
                RETURNING id
            """, (task_id, tenant_id, json.dumps(payload), max_attempts, priority)).fetchone()
        return row is not None

    def claim(self, worker_id: str, lease_seconds: int = 180) -> Job | None:
        if not worker_id or lease_seconds < 15:
            raise ValueError("worker_id is required and lease_seconds must be at least 15")
        with self._connect() as conn:
            conn.execute("""
                UPDATE aicode_jobs
                SET status='failed', lease_owner=NULL, lease_until=NULL,
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
                    lease_until=NOW() + (%s * INTERVAL '1 second'), updated_at=NOW()
                FROM candidate
                WHERE job.id=candidate.id
                RETURNING job.id, job.task_id::text, job.tenant_id, job.payload,
                          job.attempts, job.max_attempts, job.lease_owner
            """, (worker_id, lease_seconds)).fetchone()
        if row is None:
            return None
        payload = row[3] if isinstance(row[3], dict) else json.loads(row[3])
        return Job(row[0], row[1], row[2], payload, row[4], row[5], row[6])

    def heartbeat(self, job_id: int, tenant_id: str, worker_id: str,
                  lease_seconds: int = 180) -> bool:
        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE aicode_jobs SET lease_until=NOW() + (%s * INTERVAL '1 second'),
                    updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s AND status='running'
            """, (lease_seconds, job_id, tenant_id, worker_id))
        return cur.rowcount == 1

    def complete(self, job_id: int, tenant_id: str, worker_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE aicode_jobs SET status='succeeded', lease_owner=NULL,
                    lease_until=NULL, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s AND status='running'
            """, (job_id, tenant_id, worker_id))
        return cur.rowcount == 1

    def await_approval(self, job_id: int, tenant_id: str, worker_id: str, reason: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE aicode_jobs SET status='awaiting_approval', lease_owner=NULL,
                    lease_until=NULL, last_error=%s, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s AND status='running'
            """, (reason[:4000], job_id, tenant_id, worker_id))
        return cur.rowcount == 1

    def fail(self, job_id: int, tenant_id: str, worker_id: str,
             error: str, retry_delay_seconds: int = 30) -> bool:
        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE aicode_jobs
                SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                    available_at=NOW() + (%s * INTERVAL '1 second'),
                    lease_owner=NULL, lease_until=NULL, last_error=%s, updated_at=NOW()
                WHERE id=%s AND tenant_id=%s AND lease_owner=%s AND status='running'
            """, (max(0, retry_delay_seconds), error[:4000], job_id, tenant_id, worker_id))
        return cur.rowcount == 1

    def cancel(self, task_id: str, tenant_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE aicode_jobs SET status='cancelled', lease_owner=NULL,
                    lease_until=NULL, updated_at=NOW()
                WHERE task_id=%s AND tenant_id=%s AND status IN ('queued', 'running', 'awaiting_approval')
            """, (task_id, tenant_id))
        return cur.rowcount == 1

    def get(self, task_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT task_id::text, tenant_id, payload, status, attempts,
                       max_attempts, last_error, created_at, updated_at
                FROM aicode_jobs WHERE task_id=%s AND tenant_id=%s
            """, (task_id, tenant_id)).fetchone()
        if row is None:
            return None
        payload = row[2] if isinstance(row[2], dict) else json.loads(row[2])
        return {
            "task_id": row[0], "tenant_id": row[1], "payload": payload,
            "status": row[3], "attempts": row[4], "max_attempts": row[5],
            "last_error": row[6], "created_at": row[7].isoformat(),
            "updated_at": row[8].isoformat(),
        }
