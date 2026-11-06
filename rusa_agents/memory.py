"""Tenant-scoped PostgreSQL episodic memory with RLS, expiry, and provenance."""

import json
from typing import Any


class PostgresMemoryStore:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("install requirements-agent.txt to use PostgreSQL memory") from exc
        self._psycopg = psycopg
        self.dsn = dsn

    def initialize(self) -> None:
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(99101002)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rusa_agent_memory (
                    memory_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at TIMESTAMPTZ
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rusa_memory_tenant_created
                ON rusa_agent_memory(tenant_id, namespace, created_at DESC)
                """
            )
            connection.execute("ALTER TABLE rusa_agent_memory ENABLE ROW LEVEL SECURITY")
            connection.execute("ALTER TABLE rusa_agent_memory FORCE ROW LEVEL SECURITY")
            connection.execute(
                "DROP POLICY IF EXISTS rusa_memory_tenant_isolation ON rusa_agent_memory"
            )
            connection.execute(
                """
                CREATE POLICY rusa_memory_tenant_isolation ON rusa_agent_memory
                USING (tenant_id = current_setting('rusa.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true))
                """
            )

    def check_ready(self) -> bool:
        with self._psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity,
                       EXISTS (
                           SELECT 1 FROM pg_policies
                           WHERE schemaname='public'
                             AND tablename='rusa_agent_memory'
                             AND policyname='rusa_memory_tenant_isolation'
                       )
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname='rusa_agent_memory'
                """
            ).fetchone()
        return bool(row and row[0] and row[1] and row[2])

    def store(
        self,
        tenant_id: str,
        content: str,
        *,
        namespace: str = "episodic",
        source: str,
        metadata: dict[str, Any] | None = None,
        ttl_days: int = 90,
    ) -> int:
        if not tenant_id or not content.strip() or len(content) > 8000:
            raise ValueError("tenant_id is required and memory content must contain 1 to 8000 characters")
        if not source or len(source) > 512:
            raise ValueError("memory source must contain 1 to 512 characters")
        if not 1 <= ttl_days <= 3650:
            raise ValueError("ttl_days must be between 1 and 3650")
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            row = connection.execute(
                """
                INSERT INTO rusa_agent_memory
                    (tenant_id, namespace, content, source, metadata, expires_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, now() + (%s * interval '1 day'))
                RETURNING memory_id
                """,
                (
                    tenant_id,
                    namespace,
                    content,
                    source,
                    json.dumps(metadata or {}),
                    ttl_days,
                ),
            ).fetchone()
        return int(row[0])

    def search(
        self,
        tenant_id: str,
        query: str,
        *,
        namespace: str = "episodic",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 20:
            raise ValueError("memory limit must be between 1 and 20")
        terms = " ".join(query.split()[:20])
        if not terms:
            return []
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            rows = connection.execute(
                """
                SELECT memory_id, content, source, metadata, created_at
                FROM rusa_agent_memory
                WHERE tenant_id=%s AND namespace=%s
                  AND (expires_at IS NULL OR expires_at > now())
                  AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                ORDER BY ts_rank(to_tsvector('simple', content),
                                 plainto_tsquery('simple', %s)) DESC, created_at DESC
                LIMIT %s
                """,
                (tenant_id, namespace, terms, terms, limit),
            ).fetchall()
        return [
            {
                "memory_id": row[0],
                "content": row[1],
                "source": row[2],
                "metadata": row[3],
                "created_at": row[4].isoformat() if row[4] else None,
            }
            for row in rows
        ]

    def delete_tenant(self, tenant_id: str) -> int:
        with self._psycopg.connect(self.dsn) as connection:
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            cursor = connection.execute(
                "DELETE FROM rusa_agent_memory WHERE tenant_id=%s",
                (tenant_id,),
            )
            return cursor.rowcount

    def purge_expired(self) -> int:
        with self._psycopg.connect(self.dsn) as connection:
            cursor = connection.execute(
                "DELETE FROM rusa_agent_memory WHERE expires_at IS NOT NULL AND expires_at <= now()"
            )
            return cursor.rowcount
