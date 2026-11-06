"""Tenant-scoped SQLite task memory for single-instance deployments."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class ConcurrentTaskUpdate(RuntimeError):
    """Raised when an optimistic task-memory update detects a stale version."""


class SQLiteMemory:
    """SQLite task store for development or a single application instance.

    Multi-replica production deployments should inject a transactional shared
    database adapter instead of sharing a local SQLite file.
    """

    def __init__(self, database_path: str = ".rusa-ai/aicode-memory.sqlite3") -> None:
        self.path = Path(database_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("PRAGMA foreign_keys=ON")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS task_memory (
                    tenant_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, task_id)
                )
                """
            )
            database.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_memory_repository "
                "ON task_memory(tenant_id, repository)"
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=10000")
        return database

    def save(
        self,
        tenant_id: str,
        task_id: str,
        repository: str,
        state: str,
        payload: dict[str, Any],
        expected_version: int | None = None,
    ) -> int:
        if not tenant_id.strip() or not task_id.strip():
            raise ValueError("tenant_id and task_id are required")

        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT version FROM task_memory WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id),
            ).fetchone()
            current_version = row["version"] if row else 0
            if expected_version is not None and current_version != expected_version:
                database.rollback()
                raise ConcurrentTaskUpdate(
                    f"expected version {expected_version}, found {current_version}"
                )
            next_version = current_version + 1
            database.execute(
                """
                INSERT INTO task_memory
                    (tenant_id, task_id, repository, state, payload, version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, task_id) DO UPDATE SET
                    repository=excluded.repository,
                    state=excluded.state,
                    payload=excluded.payload,
                    version=excluded.version,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    tenant_id,
                    task_id,
                    repository,
                    state,
                    serialized,
                    next_version,
                ),
            )
            database.commit()
        return next_version

    def get(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as database:
            row = database.execute(
                """
                SELECT tenant_id, task_id, repository, state, payload, version, updated_at
                FROM task_memory
                WHERE tenant_id=? AND task_id=?
                """,
                (tenant_id, task_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item
