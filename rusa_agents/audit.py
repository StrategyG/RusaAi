"""Development JSONL audit and PostgreSQL hash-chained audit storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit


class AuditLog:
    """Single-instance append-only JSONL audit adapter for local development."""

    def __init__(self, path: str = ".rusa-ai/aicode-audit.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def record(
        self,
        event: str,
        task_id: str,
        actor: str,
        details: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        with self._lock, self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                previous_hash = ""
                for line in stream:
                    if line.strip():
                        previous_hash = json.loads(line)["entry_hash"]
                payload = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tenant_id": tenant_id,
                    "event": event,
                    "task_id": task_id,
                    "actor": actor,
                    "details": details or {},
                    "previous_hash": previous_hash,
                }
                entry_hash = hashlib.sha256(self._canonical(payload).encode("utf-8")).hexdigest()
                entry = {**payload, "entry_hash": entry_hash}
                stream.seek(0, os.SEEK_END)
                stream.write(self._canonical(entry) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                return entry
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def verify(self) -> bool:
        previous_hash = ""
        if not self.path.exists():
            return True
        with self._lock, self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                entry = json.loads(line)
                supplied_hash = entry.pop("entry_hash", None)
                if entry.get("previous_hash") != previous_hash:
                    return False
                expected_hash = hashlib.sha256(
                    self._canonical(entry).encode("utf-8")
                ).hexdigest()
                if supplied_hash != expected_hash:
                    return False
                previous_hash = supplied_hash
        return True


class PostgresAuditLog:
    """Shared PostgreSQL audit chain serialized with a transaction advisory lock."""

    _CHAIN_LOCK_ID = 740_221_901

    def __init__(self, dsn: str) -> None:
        parsed = urlsplit(dsn)
        if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL connection URL")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgreSQL audit storage") from exc
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
            db.execute("SELECT pg_advisory_xact_lock(%s)", (812450732,))
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rusa_agent_audit_events (
                    sequence BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            db.execute(
                """
                CREATE OR REPLACE FUNCTION rusa_agent_audit_reject_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'RUSA-AI audit events are append-only';
                END;
                $$
                """
            )
            trigger_exists = db.execute(
                """
                SELECT 1 FROM pg_trigger
                WHERE tgrelid='rusa_agent_audit_events'::regclass
                  AND tgname='rusa_agent_audit_no_mutation'
                  AND NOT tgisinternal
                """
            ).fetchone()
            if trigger_exists is None:
                db.execute(
                    """
                    CREATE TRIGGER rusa_agent_audit_no_mutation
                    BEFORE UPDATE OR DELETE ON rusa_agent_audit_events
                    FOR EACH ROW EXECUTE FUNCTION rusa_agent_audit_reject_mutation()
                    """
                )
            truncate_trigger_exists = db.execute(
                """
                SELECT 1 FROM pg_trigger
                WHERE tgrelid='rusa_agent_audit_events'::regclass
                  AND tgname='rusa_agent_audit_no_truncate'
                  AND NOT tgisinternal
                """
            ).fetchone()
            if truncate_trigger_exists is None:
                db.execute(
                    """
                    CREATE TRIGGER rusa_agent_audit_no_truncate
                    BEFORE TRUNCATE ON rusa_agent_audit_events
                    FOR EACH STATEMENT EXECUTE FUNCTION rusa_agent_audit_reject_mutation()
                    """
                )

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def record(
        self,
        event: str,
        task_id: str,
        actor: str,
        details: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        with self._connect() as db:
            with db.transaction():
                db.execute("SELECT pg_advisory_xact_lock(%s)", (self._CHAIN_LOCK_ID,))
                row = db.execute(
                    "SELECT entry_hash FROM rusa_agent_audit_events "
                    "ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                previous_hash = row["entry_hash"] if row else ""
                payload = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tenant_id": tenant_id,
                    "event": event,
                    "task_id": task_id,
                    "actor": actor,
                    "details": details or {},
                    "previous_hash": previous_hash,
                }
                entry_hash = hashlib.sha256(
                    self._canonical(payload).encode("utf-8")
                ).hexdigest()
                db.execute(
                    """
                    INSERT INTO rusa_agent_audit_events
                        (tenant_id, task_id, payload, previous_hash, entry_hash)
                    VALUES (%s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        tenant_id,
                        task_id,
                        self._canonical(payload),
                        previous_hash,
                        entry_hash,
                    ),
                )
        return {**payload, "entry_hash": entry_hash}

    def verify(self) -> bool:
        previous_hash = ""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT payload, previous_hash, entry_hash
                FROM rusa_agent_audit_events ORDER BY sequence
                """
            ).fetchall()
        for row in rows:
            payload = row["payload"]
            if row["previous_hash"] != previous_hash:
                return False
            if payload.get("previous_hash") != previous_hash:
                return False
            expected = hashlib.sha256(
                self._canonical(payload).encode("utf-8")
            ).hexdigest()
            if expected != row["entry_hash"]:
                return False
            previous_hash = row["entry_hash"]
        return True
