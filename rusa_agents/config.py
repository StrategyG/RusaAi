"""Fail-closed production configuration validation."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    database_url: str | None
    audit_hmac_key: str | None
    api_keys: dict[str, str]
    workspace_root: str | None
    sandbox_image: str | None

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        raw_keys = os.getenv("RUSA_API_KEYS", "{}")
        try:
            keys = json.loads(raw_keys)
        except json.JSONDecodeError as exc:
            raise ValueError("RUSA_API_KEYS must be a JSON object mapping API keys to tenant IDs") from exc
        if not isinstance(keys, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in keys.items()):
            raise ValueError("RUSA_API_KEYS must map string API keys to string tenant IDs")
        legacy = os.getenv("RUSA_API_KEY")
        if legacy:
            keys.setdefault(legacy, os.getenv("RUSA_DEFAULT_TENANT", "default"))
        return cls(
            environment=os.getenv("ENVIRONMENT", "development").lower(),
            database_url=os.getenv("RUSA_DATABASE_URL"),
            audit_hmac_key=os.getenv("RUSA_AUDIT_HMAC_KEY"),
            api_keys=keys,
            workspace_root=os.getenv("RUSA_WORKSPACE_ROOT"),
            sandbox_image=os.getenv("RUSA_SANDBOX_IMAGE"),
        )

    def validate(self) -> None:
        if self.environment != "production":
            return
        missing = []
        if not self.database_url: missing.append("RUSA_DATABASE_URL")
        if not self.audit_hmac_key: missing.append("RUSA_AUDIT_HMAC_KEY")
        if not self.api_keys: missing.append("RUSA_API_KEYS or RUSA_API_KEY")
        if not self.workspace_root: missing.append("RUSA_WORKSPACE_ROOT")
        if not self.sandbox_image or "@sha256:" not in self.sandbox_image:
            missing.append("RUSA_SANDBOX_IMAGE pinned by immutable sha256 digest")
        if missing:
            raise RuntimeError("Production configuration is incomplete: " + ", ".join(missing))
