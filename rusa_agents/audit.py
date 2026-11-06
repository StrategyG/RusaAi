"""Append-only, hash-chained JSONL audit log.

The hash chain makes accidental or unauthorized edits detectable. Production
deployments should additionally forward events to immutable remote storage.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class AuditLog:
    def __init__(self, path: str = ".rusa-ai/aicode-audit.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def record(
        self,
        event: str,
        task_id: str,
        actor: str,
        details: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        with self._lock, self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.seek(0)
                previous_hash = ""
                for line in stream:
                    if line.strip():
                        previous_hash = json.loads(line)["entry_hash"]
                payload = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tenant_id": tenant_id,
                    "event": event,
                    "task_id": task_id,
                    "actor": actor,
                    "details": details or {},
                    "previous_hash": previous_hash,
                }
                entry_hash = hashlib.sha256(self._canonical(payload).encode("utf-8")).hexdigest()
                entry = {**payload, "entry_hash": entry_hash}
                stream.seek(0, os.SEEK_END)
                stream.write(self._canonical(entry) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                return entry
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def verify(self) -> bool:
        previous_hash = ""
        if not self.path.exists():
            return True
        with self._lock, self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                entry = json.loads(line)
                supplied_hash = entry.pop("entry_hash", None)
                if entry.get("previous_hash") != previous_hash:
                    return False
                expected_hash = hashlib.sha256(
                    self._canonical(entry).encode("utf-8")
                ).hexdigest()
                if supplied_hash != expected_hash:
                    return False
                previous_hash = supplied_hash
        return True
