"""SQLite-backed append-only, hash-chained audit events."""
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


class AuditLog:
    def __init__(self, database_path: str = ".rusa-ai/agent-audit.sqlite3") -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_no_update
                BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END
                """
            )
            db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_no_delete
                BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def append(
        self,
        event_id: str,
        event_type: str,
        tenant_id: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as db:
            row = db.execute(
                "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_hash = row[0] if row else GENESIS_HASH
            digest_input = "|".join(
                (previous_hash, event_id, event_type, tenant_id, actor_id, canonical)
            )
            event_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
            db.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, tenant_id, actor_id, payload,
                     previous_hash, event_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    tenant_id,
                    actor_id,
                    canonical,
                    previous_hash,
                    event_hash,
                ),
            )
        return event_hash

    def verify_chain(self) -> bool:
        previous_hash = GENESIS_HASH
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT event_id, event_type, tenant_id, actor_id, payload,
                       previous_hash, event_hash
                FROM audit_events ORDER BY sequence
                """
            ).fetchall()
        for row in rows:
            event_id, event_type, tenant_id, actor_id, payload, stored_previous, stored_hash = row
            if stored_previous != previous_hash:
                return False
            digest_input = "|".join(
                (
                    previous_hash,
                    event_id,
                    event_type,
                    tenant_id,
                    actor_id,
                    payload,
                )
            )
            expected = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
            if expected != stored_hash:
                return False
            previous_hash = stored_hash
        return True
