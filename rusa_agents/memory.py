"""SQLite task memory with WAL, bounded payloads, and repository scoping."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any

class SQLiteMemory:
    def __init__(self,database_path: str=".rusa-ai/aicode-memory.sqlite3"):
        self.path=Path(database_path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=RLock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS task_memory (
                task_id TEXT PRIMARY KEY, repository TEXT NOT NULL, state TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_repo ON task_memory(repository)")
    def _connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        return db
    def save(self,task_id: str,repository: str,state: str,payload: dict[str,Any]):
        encoded=json.dumps(payload,ensure_ascii=False,separators=(",",":"),default=str)
        if len(encoded.encode())>1000000:
            raise ValueError("task memory payload exceeds 1 MB")
        with self._lock,self._connect() as db:
            db.execute("""INSERT INTO task_memory(task_id,repository,state,payload) VALUES(?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET state=excluded.state,payload=excluded.payload,
                updated_at=CURRENT_TIMESTAMP""",(task_id,repository,state,encoded))
    def get(self,task_id: str,repository: str|None=None):
        with self._lock,self._connect() as db:
            if repository is None:
                row=db.execute("SELECT * FROM task_memory WHERE task_id=?",(task_id,)).fetchone()
            else:
                row=db.execute("SELECT * FROM task_memory WHERE task_id=? AND repository=?",(task_id,repository)).fetchone()
        if row is None:
            return None
        item=dict(row)
        item["payload"]=json.loads(item["payload"])
        return item
