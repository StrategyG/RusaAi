"""Append-only hash-chained audit trail with optional mandatory HMAC signing."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class AuditLog:
    def __init__(
        self,
        path: str = ".rusa-ai/aicode-audit.jsonl",
        hmac_key: str | None = None,
        require_hmac: bool = False,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        configured_key = hmac_key or os.getenv("RUSA_AUDIT_HMAC_KEY")
        self._key = configured_key.encode("utf-8") if configured_key else None
        if require_hmac and self._key is None:
            raise RuntimeError("RUSA_AUDIT_HMAC_KEY is required for production audit logging")
        if self.path.exists() and not self.verify():
            raise RuntimeError("Existing audit log failed integrity verification")

    def _digest(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if self._key:
            return hmac.new(self._key, canonical, hashlib.sha256).hexdigest()
        return hashlib.sha256(canonical).hexdigest()

    def verify(self) -> bool:
        previous = "GENESIS"
        if not self.path.exists():
            return True
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    recorded = row.pop("record_hash")
                    if row.get("previous_hash") != previous:
                        return False
                    expected = self._digest(row)
                    if not hmac.compare_digest(recorded, expected):
                        return False
                    previous = recorded
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def record(
        self,
        event: str,
        task_id: str,
        actor: str,
        details: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> None:
        with self._lock:
            previous = "GENESIS"
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        previous = json.loads(line)["record_hash"]
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "task_id": task_id,
                "tenant_id": tenant_id,
                "actor": actor,
                "details": details or {},
                "previous_hash": previous,
            }
            row["record_hash"] = self._digest(row)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str, sort_keys=True) + "\n")
