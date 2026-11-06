"""Tenant-scoped PostgreSQL fixed-window API rate limiter."""
from __future__ import annotations

import os
import time


class PostgresRateLimiter:
    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required for tenant-scoped database access")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL driver is required for rate limiting") from exc
        return psycopg.connect(self.dsn)

    def initialize(self) -> None:
        with self._connect() as conn:
            if os.getenv("ENVIRONMENT", "development").lower() == "production":
                exists = conn.execute("SELECT to_regclass('public.aicode_rate_limits')").fetchone()
                if not exists or exists[0] is None:
                    raise RuntimeError("Production rate-limit schema is missing; apply database migrations")
                return
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aicode_rate_limits (
                    tenant_id TEXT NOT NULL,
                    route_key TEXT NOT NULL,
                    window_start BIGINT NOT NULL,
                    request_count INTEGER NOT NULL CHECK (request_count > 0),
                    PRIMARY KEY (tenant_id, route_key, window_start)
                )
            """)

    def hit(
        self,
        tenant_id: str,
        route_key: str,
        limit: int,
        window_seconds: int = 60,
    ) -> tuple[bool, int]:
        if not tenant_id or not route_key or limit < 1 or window_seconds < 1:
            raise ValueError("Tenant, route, positive limit, and positive window are required")
        now = int(time.time())
        window_start = (now // window_seconds) * window_seconds
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute("""
                INSERT INTO aicode_rate_limits(tenant_id, route_key, window_start, request_count)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (tenant_id, route_key, window_start)
                DO UPDATE SET request_count=aicode_rate_limits.request_count + 1
                RETURNING request_count
            """, (tenant_id, route_key, window_start)).fetchone()
            conn.execute(
                "DELETE FROM aicode_rate_limits WHERE window_start < %s",
                (window_start - (2 * window_seconds),),
            )
        count = row[0]
        retry_after = max(1, window_start + window_seconds - now)
        return count <= limit, retry_after
