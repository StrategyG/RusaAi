"""Tenant- and project-scoped SQLite memory adapter.

SQLite is suitable for local/single-instance deployments. Multi-replica production
must replace this adapter with a transactional shared database implementation.
"""
import json
import sqlite3
from pathlib import Path
from typing import Any


class SQLiteMemory:
    def __init__(self, database_path: str = ".rusa-ai/agent-memory.sqlite3") -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS task_records (
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, project_id, task_id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_reference TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, project_id, memory_id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_scope "
                "ON memories(tenant_id, project_id, memory_type)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def save_task(
        self,
        tenant_id: str,
        project_id: str,
        task_id: str,
        state: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO task_records
                    (tenant_id, project_id, task_id, state, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, project_id, task_id) DO UPDATE SET
                    state=excluded.state,
                    payload=excluded.payload,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    tenant_id,
                    project_id,
                    task_id,
                    state,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    def get_task(
        self, tenant_id: str, project_id: str, task_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT task_id, state, payload, updated_at
                FROM task_records
                WHERE tenant_id=? AND project_id=? AND task_id=?
                """,
                (tenant_id, project_id, task_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def put_memory(
        self,
        tenant_id: str,
        project_id: str,
        memory_id: str,
        memory_type: str,
        content: str,
        source_reference: str | None = None,
    ) -> None:
        if not all((tenant_id, project_id, memory_id, memory_type, content)):
            raise ValueError("memory scope, ID, type, and content are required")
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO memories
                    (tenant_id, project_id, memory_id, memory_type, content, source_reference)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, project_id, memory_id) DO UPDATE SET
                    memory_type=excluded.memory_type,
                    content=excluded.content,
                    source_reference=excluded.source_reference
                """,
                (
                    tenant_id,
                    project_id,
                    memory_id,
                    memory_type,
                    content,
                    source_reference,
                ),
            )

    def search_memory(
        self, tenant_id: str, project_id: str, memory_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        query = (
            "SELECT memory_id, memory_type, content, source_reference, created_at "
            "FROM memories WHERE tenant_id=? AND project_id=?"
        )
        params: list[Any] = [tenant_id, project_id]
        if memory_type is not None:
            query += " AND memory_type=?"
            params.append(memory_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]
