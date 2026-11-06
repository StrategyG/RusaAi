"""Tenant-scoped SQLite development memory and PostgreSQL shared memory."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SQLiteMemory:
    """Single-instance development/test adapter; not for multi-replica production."""

    def __init__(self, database_path: str = ".rusa-ai/aicode-memory.sqlite3") -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        with self._connect() as db:
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
        with self._connect() as db:
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


class PostgresMemory:
    """Shared tenant-scoped memory for horizontally scaled agent workers."""

    def __init__(self, dsn: str) -> None:
        parsed = urlsplit(dsn)
        if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL connection URL")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgreSQL memory") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS rusa_agent_memory (
                    tenant_id TEXT NOT NULL,
                    task_id UUID NOT NULL,
                    repository TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, task_id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rusa_agent_memory_repository "
                "ON rusa_agent_memory(tenant_id, repository, updated_at DESC)"
            )

    def _connect(self):
        return self._psycopg.connect(
            self.dsn,
            connect_timeout=5,
            row_factory=self._dict_row,
        )

    def save(
        self,
        task_id: str,
        repository: str,
        state: str,
        payload: dict[str, Any],
        tenant_id: str = "default",
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO rusa_agent_memory
                    (tenant_id, task_id, repository, state, payload)
                VALUES (%s, %s::uuid, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, task_id) DO UPDATE SET
                    repository=excluded.repository,
                    state=excluded.state,
                    payload=excluded.payload,
                    updated_at=now()
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
        with self._connect() as db:
            row = db.execute(
                """
                SELECT task_id, tenant_id, repository, state, payload, updated_at
                FROM rusa_agent_memory WHERE tenant_id=%s AND task_id=%s::uuid
                """,
                (tenant_id, task_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["task_id"] = str(result["task_id"])
        return result
