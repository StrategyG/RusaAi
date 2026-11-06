"""Append-only JSONL audit trail with hash chaining."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path
from threading import RLock
from typing import Any

class AuditLog:
    def __init__(self,path: str=".rusa-ai/aicode-audit.jsonl"):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self._lock=RLock()
    def record(self,event: str,task_id: str,actor: str,details: dict[str,Any]|None=None):
        if not event or not task_id or not actor:
            raise ValueError("event, task_id, and actor are required")
        with self._lock:
            previous=""
            if self.path.exists():
                with self.path.open("rb") as stream:
                    for line in stream:
                        if line.strip():
                            previous=json.loads(line).get("hash","")
            row={"timestamp":datetime.now(timezone.utc).isoformat(),"event":event,"task_id":task_id,"actor":actor,"details":details or {},"previous_hash":previous}
            canonical=json.dumps(row,sort_keys=True,separators=(",",":"),default=str)
            row["hash"]=hashlib.sha256(canonical.encode()).hexdigest()
            with self.path.open("a",encoding="utf-8") as stream:
                stream.write(json.dumps(row,ensure_ascii=False,separators=(",",":"),default=str)+"\n")
            return row
