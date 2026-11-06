"""PostgreSQL access with transaction-local tenant context and enforced RLS."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator


class DatabaseConfigurationError(RuntimeError):
    pass


class TenantDatabase:
    def __init__(self, dsn: str):
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise DatabaseConfigurationError("PostgreSQL driver is unavailable") from exc
        max_size = int(os.getenv("RUSA_DB_POOL_MAX", "10"))
        if max_size < 1:
            raise DatabaseConfigurationError("RUSA_DB_POOL_MAX must be positive")
        self._pool = ConnectionPool(dsn, min_size=1, max_size=max_size,
                                   timeout=5, kwargs={"row_factory": dict_row}, open=False)

    def open(self) -> None:
        self._pool.open(wait=True, timeout=10)
        try:
            self.verify_security()
        except Exception:
            self._pool.close()
            raise

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def tenant_connection(self, tenant_id: str) -> Iterator[Any]:
        # Transaction-local context is reset before a pooled connection is reused.
        with self._pool.connection() as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            yield conn

    def verify_security(self) -> None:
        with self._pool.connection() as conn:
            role = conn.execute(
                "SELECT current_user AS name, r.rolsuper, r.rolbypassrls "
                "FROM pg_roles r WHERE r.rolname = current_user"
            ).fetchone()
            table = conn.execute(
                "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                "pg_get_userbyid(c.relowner) AS owner "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'tenant_records' "
                "AND c.relkind = 'r'"
            ).fetchone()
            policies = conn.execute(
                "SELECT policyname, permissive, cmd, roles, qual, with_check "
                "FROM pg_policies WHERE schemaname = 'public' AND tablename = 'tenant_records'"
            ).fetchall()

        if not role or role["rolsuper"] or role["rolbypassrls"]:
            raise DatabaseConfigurationError("Application database role must not bypass RLS")
        if not table or not table["relrowsecurity"] or not table["relforcerowsecurity"]:
            raise DatabaseConfigurationError("tenant_records must have ENABLE and FORCE ROW LEVEL SECURITY")
        if table["owner"] == role["name"]:
            raise DatabaseConfigurationError("Application database role must not own tenant_records")
        if len(policies) != 1:
            raise DatabaseConfigurationError("tenant_records must have exactly one reviewed RLS policy")
        policy = policies[0]
        if (policy["policyname"] != "tenant_records_isolation"
                or policy["permissive"] != "PERMISSIVE" or policy["cmd"] != "ALL"):
            raise DatabaseConfigurationError("tenant_records RLS policy does not match the reviewed policy")
        if list(policy["roles"]) != ["rusa_app"]:
            raise DatabaseConfigurationError("tenant_records RLS policy has an unexpected role scope")
        expected = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')"
        for expression in (policy["qual"] or "", policy["with_check"] or ""):
            normalized = " ".join(expression.split()).replace("::text", "").strip("() ")
            if normalized != expected:
                raise DatabaseConfigurationError("tenant_records RLS policy expression is not the reviewed tenant predicate")

    def create_record(self, tenant_id: str, subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.tenant_connection(tenant_id) as conn:
            return conn.execute(
                "INSERT INTO public.tenant_records (tenant_id, owner_subject, payload) "
                "VALUES (%s, %s, %s::jsonb) RETURNING id, payload, created_at",
                (tenant_id, subject, json.dumps(payload)),
            ).fetchone()

    def list_records(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.tenant_connection(tenant_id) as conn:
            return conn.execute(
                "SELECT id, payload, created_at FROM public.tenant_records "
                "ORDER BY created_at DESC LIMIT 100"
            ).fetchall()

    def get_record(self, tenant_id: str, record_id: str) -> dict[str, Any] | None:
        with self.tenant_connection(tenant_id) as conn:
            return conn.execute(
                "SELECT id, payload, created_at FROM public.tenant_records WHERE id = %s",
                (record_id,),
            ).fetchone()
