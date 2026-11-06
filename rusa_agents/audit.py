"""Append-only hash-chained audit trail with optional mandatory HMAC signing."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


def record_task_policy(
    tenant_id: str | None,
    task_id: str | None,
    actor: str,
    action: str,
    allowed: bool,
    *,
    sink: PostgresTaskPolicyAudit | None = None,
    required: bool = False,
) -> None:
    """Persist and emit a redacted task authorization decision."""
    event = {
        "event": "task.policy",
        "tenant_id": tenant_id,
        "task_id": task_id,
        "actor": actor,
        "action": action,
        "decision": "allow" if allowed else "deny",
    }
    logging.getLogger("rusa-ai.task-policy").info(
        "%s", json.dumps(event, separators=(",", ":"), sort_keys=True)
    )
    if sink is not None:
        try:
            sink.record(tenant_id, task_id, actor, action, event["decision"])
        except Exception:
            logging.getLogger("rusa-ai.task-policy").error(
                "Task policy audit persistence failed"
            )
            if required:
                raise RuntimeError("Task policy audit store is unavailable") from None
    elif required:
        raise RuntimeError("Task policy audit store is not configured")


class PostgresTaskPolicyAudit:
    """Append-only PostgreSQL task authorization event store protected by RLS."""

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQL audit DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use the task policy audit store") from exc
        return psycopg.connect(self.dsn, connect_timeout=5)

    def verify_ready(self, runtime_role: str) -> None:
        if runtime_role not in {"rusa_api_runtime", "rusa_worker_runtime"}:
            raise ValueError("Unexpected task policy audit runtime role")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT current_user,
                       db_role.rolsuper,
                       db_role.rolbypassrls,
                       pg_has_role(current_user, %s, 'member'),
                       table_info.relrowsecurity,
                       table_info.relforcerowsecurity,
                       pg_get_userbyid(table_info.relowner),
                       row_security_active('public.aicode_task_policy_events'),
                       has_table_privilege(current_user, 'public.aicode_task_policy_events', 'INSERT'),
                       has_table_privilege(current_user, 'public.aicode_task_policy_events', 'SELECT'),
                       has_table_privilege(current_user, 'public.aicode_task_policy_events', 'UPDATE'),
                       has_table_privilege(current_user, 'public.aicode_task_policy_events', 'DELETE'),
                       EXISTS (
                           SELECT 1 FROM pg_policies
                           WHERE schemaname='public'
                             AND tablename='aicode_task_policy_events'
                             AND policyname = CASE
                                 WHEN %s = 'rusa_api_runtime'
                                 THEN 'aicode_task_policy_events_api_insert'
                                 ELSE 'aicode_task_policy_events_worker_insert'
                             END
                             AND cmd='INSERT'
                             AND permissive='PERMISSIVE'
                       ),
                       EXISTS (
                           SELECT 1 FROM pg_trigger
                           WHERE tgrelid='public.aicode_task_policy_events'::regclass
                             AND NOT tgisinternal
                             AND tgname='trg_aicode_task_policy_events_immutable'
                       ),
                       EXISTS (
                           SELECT 1 FROM pg_trigger
                           WHERE tgrelid='public.aicode_task_policy_events'::regclass
                             AND NOT tgisinternal
                             AND tgname='trg_aicode_task_policy_events_no_truncate'
                       )
                FROM pg_roles AS db_role
                CROSS JOIN pg_class AS table_info
                JOIN pg_namespace AS namespace ON namespace.oid=table_info.relnamespace
                WHERE db_role.rolname=current_user
                  AND namespace.nspname='public'
                  AND table_info.relname='aicode_task_policy_events'
                  AND table_info.relkind='r'
                """,
                (runtime_role, runtime_role),
            ).fetchone()
        if (
            not row
            or row[1]
            or row[2]
            or not row[3]
            or not row[4]
            or not row[5]
            or row[6] == row[0]
            or not row[7]
            or not row[8]
            or row[9]
            or row[10]
            or row[11]
            or not row[12]
            or not row[13]
            or not row[14]
        ):
            raise RuntimeError(
                "Task policy audit table, RLS, append-only triggers, or runtime privileges are not ready"
            )

    def ping(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)

    def record(
        self,
        tenant_id: str | None,
        task_id: str | None,
        actor: str,
        action: str,
        decision: str,
    ) -> None:
        if decision not in {"allow", "deny"}:
            raise ValueError("Task policy decision must be allow or deny")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ValueError("Task policy actor is invalid")
        if not isinstance(action, str) or not action.strip() or len(action) > 64:
            raise ValueError("Task policy action is invalid")
        with self._connect() as conn:
            conn.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                (tenant_id or "",),
            )
            conn.execute(
                """
                INSERT INTO public.aicode_task_policy_events
                    (tenant_id, task_id, actor, action, decision)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (tenant_id, task_id, actor, action, decision),
            )


class AuditLog:
    def __init__(
        self,
        path: str = ".rusa-ai/aicode-audit.jsonl",
        hmac_key: str | None = None,
        require_hmac: bool = False,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        configured_key = hmac_key or os.getenv("RUSA_AUDIT_HMAC_KEY")
        self._key = configured_key.encode("utf-8") if configured_key else None
        if require_hmac and (self._key is None or len(self._key) < 32):
            raise RuntimeError("RUSA_AUDIT_HMAC_KEY must contain at least 32 bytes in production")
        if self.path.exists() and not self.verify():
            raise RuntimeError("Existing audit log failed integrity verification")

    def _digest(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if self._key:
            return hmac.new(self._key, canonical, hashlib.sha256).hexdigest()
        return hashlib.sha256(canonical).hexdigest()

    def verify(self) -> bool:
        previous = "GENESIS"
        if not self.path.exists():
            return True
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    recorded = row.pop("record_hash")
                    if row.get("previous_hash") != previous:
                        return False
                    expected = self._digest(row)
                    if not hmac.compare_digest(recorded, expected):
                        return False
                    previous = recorded
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def record(
        self,
        event: str,
        task_id: str,
        actor: str,
        details: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> None:
        with self._lock:
            if self.path.exists() and not self.verify():
                raise RuntimeError("Audit log integrity check failed; refusing to append")
            previous = "GENESIS"
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        previous = json.loads(line)["record_hash"]
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "task_id": task_id,
                "tenant_id": tenant_id,
                "actor": actor,
                "details": details or {},
                "previous_hash": previous,
            }
            row["record_hash"] = self._digest(row)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str, sort_keys=True) + "\n")


class PostgresAuditLog:
    """Central HMAC-chained audit store for multi-worker deployments."""

    def __init__(self, dsn: str, hmac_key: str):
        if not dsn or not hmac_key or len(hmac_key.encode("utf-8")) < 32:
            raise ValueError("PostgreSQL DSN and an audit HMAC key of at least 32 bytes are required")
        self.dsn = dsn
        self._key = hmac_key.encode("utf-8")

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use PostgresAuditLog") from exc
        return psycopg.connect(self.dsn)

    def initialize(self) -> None:
        with self._connect() as conn:
            if os.getenv("ENVIRONMENT", "development").lower() == "production":
                trigger_rows = conn.execute("""
                    SELECT tgname FROM pg_trigger
                    WHERE tgrelid='public.aicode_audit_events'::regclass AND NOT tgisinternal
                """).fetchall()
                trigger_names = {row[0] for row in trigger_rows}
                if not {"trg_aicode_audit_immutable", "trg_aicode_audit_no_truncate"}.issubset(trigger_names):
                    raise RuntimeError("Production audit immutability triggers are missing; apply migrations/001_aicode_core.sql")
                return
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aicode_audit_events (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    event TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details JSONB NOT NULL,
                    previous_hash TEXT NOT NULL,
                    record_hash TEXT NOT NULL UNIQUE
                )
            """)
            conn.execute("""
                CREATE OR REPLACE FUNCTION rusa_audit_immutable() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'aicode_audit_events is append-only';
                END;
                $$
            """)
            conn.execute("DROP TRIGGER IF EXISTS trg_aicode_audit_immutable ON aicode_audit_events")
            conn.execute("""
                CREATE TRIGGER trg_aicode_audit_immutable
                BEFORE UPDATE OR DELETE ON aicode_audit_events
                FOR EACH ROW EXECUTE FUNCTION rusa_audit_immutable()
            """)
            conn.execute("DROP TRIGGER IF EXISTS trg_aicode_audit_no_truncate ON aicode_audit_events")
            conn.execute("""
                CREATE TRIGGER trg_aicode_audit_no_truncate
                BEFORE TRUNCATE ON aicode_audit_events
                FOR EACH STATEMENT EXECUTE FUNCTION rusa_audit_immutable()
            """)

    def _digest(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hmac.new(self._key, canonical, hashlib.sha256).hexdigest()

    def record(self, event: str, task_id: str, actor: str,
               details: dict[str, Any] | None = None, tenant_id: str = "default") -> None:
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(741203091)")
            previous_row = conn.execute(
                "SELECT record_hash FROM aicode_audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = previous_row[0] if previous_row else "GENESIS"
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event, "task_id": task_id, "tenant_id": tenant_id,
                "actor": actor, "details": details or {}, "previous_hash": previous,
            }
            digest = self._digest(row)
            conn.execute("""
                INSERT INTO aicode_audit_events
                    (timestamp, event, task_id, tenant_id, actor, details, previous_hash, record_hash)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """, (
                row["timestamp"], event, task_id, tenant_id, actor,
                json.dumps(row["details"], ensure_ascii=False, default=str), previous, digest,
            ))

    def verify(self) -> bool:
        previous = "GENESIS"
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT timestamp, event, task_id, tenant_id, actor, details, previous_hash, record_hash
                FROM aicode_audit_events ORDER BY id ASC
            """).fetchall()
        for row in rows:
            details = row[5] if isinstance(row[5], dict) else json.loads(row[5])
            payload = {
                "timestamp": row[0].isoformat(), "event": row[1], "task_id": row[2],
                "tenant_id": row[3], "actor": row[4], "details": details,
                "previous_hash": row[6],
            }
            if row[6] != previous or not hmac.compare_digest(row[7], self._digest(payload)):
                return False
            previous = row[7]
        return True
