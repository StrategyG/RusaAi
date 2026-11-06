"""HMAC-chained JSONL audit log; use remote immutable storage for production retention."""

import fcntl
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .github import redact_sensitive_text


_SENSITIVE_KEYS = (
    "token", "secret", "password", "api_key", "private_key",
    "credential", "authorization",
)


def _safe_audit_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if depth >= 6:
        return "[nested audit data omitted]"
    if isinstance(value, str):
        return redact_sensitive_text(value)[:8000]
    if isinstance(value, dict):
        return {
            str(child_key)[:128]: _safe_audit_value(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child_value in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_audit_value(item, depth=depth + 1)
            for item in list(value)[:100]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:8000]


class AuditIntegrityError(RuntimeError):
    """Raised when an audit record is modified, reordered, or removed."""


class HMACAuditLog:
    def __init__(self, path: str, key: str | bytes) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.key = key.encode("utf-8") if isinstance(key, str) else key
        if len(self.key) < 32:
            raise ValueError("audit HMAC key must contain at least 32 bytes")
        self._lock = RLock()
        if self.path.exists():
            os.chmod(self.path, 0o600)

    @staticmethod
    def _canonical(value: dict[str, Any]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    def _signature(self, payload: dict[str, Any]) -> str:
        return hmac.new(self.key, self._canonical(payload), hashlib.sha256).hexdigest()

    def _read_verified(self, stream: Any) -> str | None:
        previous = None
        stream.seek(0)
        for line_number, line in enumerate(stream, start=1):
            try:
                row = json.loads(line)
                signature = row.pop("signature")
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise AuditIntegrityError(f"invalid audit record at line {line_number}") from exc
            if row.get("previous_signature") != previous:
                raise AuditIntegrityError(f"audit chain mismatch at line {line_number}")
            if not hmac.compare_digest(str(signature), self._signature(row)):
                raise AuditIntegrityError(f"audit signature mismatch at line {line_number}")
            previous = signature
        return previous

    def record(
        self,
        event: str,
        *,
        tenant_id: str,
        task_id: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "actor": actor,
            "details": _safe_audit_value(details or {}),
        }
        with self._lock, self.path.open("a+", encoding="utf-8") as stream:
            os.chmod(self.path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                row["previous_signature"] = self._read_verified(stream)
                row["signature"] = self._signature(row)
                stream.seek(0, os.SEEK_END)
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def verify(self) -> bool:
        if not self.path.exists():
            return True
        with self._lock, self.path.open("r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                self._read_verified(stream)
                return True
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
