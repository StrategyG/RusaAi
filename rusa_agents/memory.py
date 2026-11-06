"""SQLite-backed task memory."""
import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any

class SQLiteMemory:
    def __init__(self, database_path: str=".rusa-ai/aicode-memory.sqlite3"):
        self.path=Path(database_path); self.path.parent.mkdir(parents=True,exist_ok=True); self._lock=RLock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS task_memory (task_id TEXT PRIMARY KEY, repository TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_repo ON task_memory(repository)")
    def _connect(self):
        db=sqlite3.connect(self.path,timeout=10); db.row_factory=sqlite3.Row; return db
    def save(self,task_id: str,repository: str,state: str,payload: dict[str,Any]):
        with self._lock,self._connect() as db:
            db.execute("INSERT INTO task_memory(task_id,repository,state,payload) VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",(task_id,repository,state,json.dumps(payload,ensure_ascii=False)))
    def get(self,task_id: str):
        with self._lock,self._connect() as db:
            row=db.execute("SELECT task_id,repository,state,payload,updated_at FROM task_memory WHERE task_id=?",(task_id,)).fetchone()
        if row is None: return None
        item=dict(row); item["payload"]=json.loads(item["payload"]); return item
