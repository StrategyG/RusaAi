"""Tenant-scoped SQLite memory for development and single-node deployments."""
import json
import os
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any


class SQLiteMemory:
    def __init__(self, database_path: str = ".rusa-ai/aicode-memory.sqlite3"):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS task_memory ("
                "task_id TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT 'default', "
                "repository TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY(task_id, tenant_id))"
            )
            columns = {row["name"]: row for row in db.execute("PRAGMA table_info(task_memory)")}
            if "tenant_id" not in columns:
                db.execute("ALTER TABLE task_memory ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
                columns = {row["name"]: row for row in db.execute("PRAGMA table_info(task_memory)")}
            if columns["task_id"]["pk"] and not columns["tenant_id"]["pk"]:
                db.execute("ALTER TABLE task_memory RENAME TO task_memory_legacy")
                db.execute(
                    "CREATE TABLE task_memory (task_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                    "repository TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(task_id, tenant_id))"
                )
                db.execute(
                    "INSERT INTO task_memory(task_id, tenant_id, repository, state, payload, updated_at) "
                    "SELECT task_id, tenant_id, repository, state, payload, updated_at FROM task_memory_legacy"
                )
                db.execute("DROP TABLE task_memory_legacy")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_tenant_repo "
                "ON task_memory(tenant_id, repository)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def save(
        self, task_id: str, repository: str, state: str,
        payload: dict[str, Any], tenant_id: str = "default",
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO task_memory(task_id, tenant_id, repository, state, payload) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id, tenant_id) DO UPDATE SET "
                "repository=excluded.repository, state=excluded.state, "
                "payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
                (task_id, tenant_id, repository, state, json.dumps(payload, ensure_ascii=False)),
            )

    def get(self, task_id: str, tenant_id: str = "default") -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT task_id, tenant_id, repository, state, payload, updated_at "
                "FROM task_memory WHERE task_id=? AND tenant_id=?",
                (task_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item


class PostgresMemory:
    """Shared tenant-aware memory for multi-worker production deployments."""

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use PostgresMemory") from exc
        return psycopg.connect(self.dsn)

    def initialize(self) -> None:
        with self._connect() as conn:
            if os.getenv("ENVIRONMENT", "development").lower() == "production":
                exists = conn.execute("SELECT to_regclass('public.aicode_memory')").fetchone()
                if not exists or exists[0] is None:
                    raise RuntimeError("Production memory schema is missing; apply migrations/001_aicode_core.sql")
                return
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aicode_memory (
                    tenant_id TEXT NOT NULL,
                    task_id UUID NOT NULL,
                    repository TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (tenant_id, task_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_aicode_memory_tenant_repo
                ON aicode_memory(tenant_id, repository)
            """)

    def save(self, task_id: str, repository: str, state: str,
             payload: dict[str, Any], tenant_id: str = "default") -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO aicode_memory(tenant_id, task_id, repository, state, payload)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, task_id) DO UPDATE SET
                    repository=EXCLUDED.repository, state=EXCLUDED.state,
                    payload=EXCLUDED.payload, updated_at=NOW()
            """, (tenant_id, task_id, repository, state, json.dumps(payload, ensure_ascii=False)))

    def get(self, task_id: str, tenant_id: str = "default") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT tenant_id, task_id::text, repository, state, payload, updated_at
                FROM aicode_memory WHERE task_id=%s AND tenant_id=%s
            """, (task_id, tenant_id)).fetchone()
        if row is None:
            return None
        payload = row[4] if isinstance(row[4], dict) else json.loads(row[4])
        return {
            "tenant_id": row[0], "task_id": row[1], "repository": row[2],
            "state": row[3], "payload": payload, "updated_at": row[5].isoformat(),
        }
