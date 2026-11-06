"""Append-only hash-chained audit trail with mandatory secret redaction."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization|credential|private[_-]?key)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*")
_SECRET_ASSIGNMENT = re.compile(
    r"""(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization)(\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
)
_PROVIDER_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"
)
_DATABASE_PASSWORD = re.compile(r"(?i)((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:/@\s]+:)[^@\s]+(@)")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_CLOUD_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_STRIPE_TOKEN = re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b")
_GOOGLE_API_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|password|secret)=)[^&\s]+")


def redact_audit_value(value: Any, key: str = "") -> Any:
    """Remove credential-like values recursively before they are persisted or hashed."""
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): redact_audit_value(child_value, str(child_key))
                for child_key, child_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_audit_value(item) for item in value]
    if isinstance(value, str):
        redacted = _BEARER_VALUE.sub(r"\1[REDACTED]", value)
        redacted = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", redacted)
        redacted = _PROVIDER_TOKEN.sub("[REDACTED]", redacted)
        redacted = _DATABASE_PASSWORD.sub(r"\1[REDACTED]\2", redacted)
        redacted = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", redacted)
        redacted = _CLOUD_ACCESS_KEY.sub("[REDACTED]", redacted)
        redacted = _SLACK_TOKEN.sub("[REDACTED]", redacted)
        redacted = _STRIPE_TOKEN.sub("[REDACTED]", redacted)
        redacted = _GOOGLE_API_KEY.sub("[REDACTED]", redacted)
        redacted = _QUERY_SECRET.sub(r"\1[REDACTED]", redacted)
        return redacted
    return value


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
                "details": redact_audit_value(details or {}),
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
        safe_details = redact_audit_value(details or {})
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(741203091)")
            previous_row = conn.execute(
                "SELECT record_hash FROM aicode_audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = previous_row[0] if previous_row else "GENESIS"
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event, "task_id": task_id, "tenant_id": tenant_id,
                "actor": actor, "details": safe_details, "previous_hash": previous,
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
