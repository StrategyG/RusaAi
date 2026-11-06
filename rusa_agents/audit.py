"""Append-only JSONL audit trail."""
import json
from datetime import datetime,timezone
from pathlib import Path
from threading import RLock
from typing import Any

class AuditLog:
    def __init__(self,path: str=".rusa-ai/aicode-audit.jsonl"):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self._lock=RLock()
    def record(self,event: str,task_id: str,actor: str,details: dict[str,Any]|None=None):
        row={"timestamp":datetime.now(timezone.utc).isoformat(),"event":event,"task_id":task_id,"actor":actor,"details":details or {}}
        with self._lock,self.path.open("a",encoding="utf-8") as stream:
            stream.write(json.dumps(row,ensure_ascii=False,default=str)+"\n")
