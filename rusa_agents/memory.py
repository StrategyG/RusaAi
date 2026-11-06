"""Tenant-scoped SQLite task memory for single-node deployments and tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


class SQLiteMemory:
    def __init__(self, database_path: str = ".rusa-ai/aicode-memory.sqlite3") -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS task_memory (
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, task_id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_memory_repository "
                "ON task_memory(tenant_id, repository)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def save(
        self,
        task_id: str,
        repository: str,
        state: str,
        payload: dict[str, Any],
        tenant_id: str = "default",
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO task_memory(tenant_id, task_id, repository, state, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, task_id) DO UPDATE SET
                    repository=excluded.repository,
                    state=excluded.state,
                    payload=excluded.payload,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    tenant_id,
                    task_id,
                    repository,
                    state,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    def get(self, task_id: str, tenant_id: str = "default") -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT task_id, tenant_id, repository, state, payload, updated_at
                FROM task_memory WHERE tenant_id=? AND task_id=?
                """,
                (tenant_id, task_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item
