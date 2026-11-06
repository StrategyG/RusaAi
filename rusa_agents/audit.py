"""HMAC-chained audit log with redaction and integrity checks."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

ZERO = "0" * 64
SENSITIVE = ("token", "secret", "password", "authorization", "api_key", "credential")


class AuditIntegrityError(RuntimeError):
    pass


def _redact(value: Any, key: str = "") -> Any:
    if any(marker in key.lower() for marker in SENSITIVE):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value[:100]]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]


class AuditLog:
    def __init__(self, path: str = ".rusa-ai/aicode-audit.jsonl",
                 integrity_key: str | bytes | None = None) -> None:
        key = integrity_key or os.getenv("RUSA_AUDIT_HMAC_KEY")
        if isinstance(key, str):
            key = key.encode()
        if not key or len(key) < 32:
            raise ValueError("RUSA_AUDIT_HMAC_KEY must contain at least 32 bytes")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.key = key
        self.lock = RLock()
        self.verify()

    @staticmethod
    def _canonical(data: dict[str, Any]) -> bytes:
        return json.dumps(data, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode()

    def _last_digest(self) -> str:
        previous = ZERO
        with self.path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.endswith("\n"):
                    raise AuditIntegrityError(f"incomplete audit record {number}")
                try:
                    row = json.loads(line)
                    digest = row.pop("digest")
                except (ValueError, KeyError, TypeError) as exc:
                    raise AuditIntegrityError(f"malformed audit record {number}") from exc
                if row.get("previous_digest") != previous:
                    raise AuditIntegrityError(f"audit chain mismatch at record {number}")
                expected = hmac.new(self.key, self._canonical(row), hashlib.sha256).hexdigest()
                if not isinstance(digest, str) or not hmac.compare_digest(digest, expected):
                    raise AuditIntegrityError(f"audit signature mismatch at record {number}")
                previous = digest
        return previous

    def verify(self) -> bool:
        with self.lock:
            self._last_digest()
        return True

    def record(self, event: str, task_id: str, actor: str,
               details: dict[str, Any] | None = None) -> str:
        import fcntl
        if not event.strip() or not task_id.strip() or not actor.strip():
            raise ValueError("event, task_id and actor are required")
        with self.lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                previous = self._last_digest()
                row = {"timestamp": datetime.now(timezone.utc).isoformat(),
                       "event": event, "task_id": task_id, "actor": actor,
                       "details": _redact(details or {}), "previous_digest": previous}
                digest = hmac.new(self.key, self._canonical(row), hashlib.sha256).hexdigest()
                payload = self._canonical({**row, "digest": digest}) + b"\n"
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:])
                os.fsync(fd)
                return digest
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
