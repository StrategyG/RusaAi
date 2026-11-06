"""Fail-closed production configuration validation."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    database_url: str | None
    audit_hmac_key: str | None
    api_keys: dict[str, str]
    workspace_root: str | None
    sandbox_image: str | None
    git_mirror_root: str | None
    github_token: str | None
    model_base_url: str | None
    model_name: str
    tenant_repositories: dict[str, list[str]]

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        try:
            keys = json.loads(os.getenv("RUSA_API_KEYS", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("RUSA_API_KEYS must be a JSON object mapping API keys to tenant IDs") from exc
        if not isinstance(keys, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in keys.items()):
            raise ValueError("RUSA_API_KEYS must map string API keys to string tenant IDs")
        try:
            tenant_repositories = json.loads(os.getenv("RUSA_TENANT_REPOSITORIES", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("RUSA_TENANT_REPOSITORIES must be a JSON object") from exc
        if not isinstance(tenant_repositories, dict) or any(
            not isinstance(tenant, str) or not isinstance(repos, list)
            or any(not isinstance(repo, str) for repo in repos)
            for tenant, repos in tenant_repositories.items()
        ):
            raise ValueError("RUSA_TENANT_REPOSITORIES must map tenant IDs to repository-name arrays")
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
            git_mirror_root=os.getenv("RUSA_GIT_MIRROR_ROOT"),
            github_token=os.getenv("RUSA_GITHUB_TOKEN"),
            model_base_url=os.getenv("RUSA_MODEL_BASE_URL"),
            model_name=os.getenv("RUSA_MODEL_NAME", "rusa-ai"),
            tenant_repositories=tenant_repositories,
        )

    def validate_api(self) -> None:
        if self.environment != "production":
            return
        required = {
            "RUSA_DATABASE_URL": self.database_url,
            "RUSA_API_KEYS or RUSA_API_KEY": self.api_keys,
            "RUSA_TENANT_REPOSITORIES": self.tenant_repositories,
            "RUSA_MODEL_BASE_URL": self.model_base_url,
        }
        missing = [name for name, value in required.items() if not value]
        if any(len(key.encode("utf-8")) < 32 for key in self.api_keys):
            missing.append("API keys of at least 32 UTF-8 bytes")
        if any(not tenant.strip() for tenant in self.api_keys.values()):
            missing.append("non-empty API-key tenant mappings")
        if any(not tenant.strip() for tenant in self.tenant_repositories):
            missing.append("non-empty tenant identifiers")
        if self.database_url:
            database_url = urlparse(self.database_url)
            ssl_modes = parse_qs(database_url.query).get("sslmode", [])
            if database_url.scheme not in {"postgres", "postgresql"}:
                missing.append("RUSA_DATABASE_URL using PostgreSQL")
            if not ssl_modes or ssl_modes[-1] not in {"require", "verify-ca", "verify-full"}:
                missing.append("RUSA_DATABASE_URL with sslmode=require, verify-ca, or verify-full")
        if self.model_base_url:
            model_url = urlparse(self.model_base_url)
            if model_url.scheme != "https" or not model_url.hostname or model_url.username or model_url.password:
                missing.append("RUSA_MODEL_BASE_URL using credential-free HTTPS")
        for tenant, repositories in self.tenant_repositories.items():
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) for repository in repositories):
                missing.append(f"valid owner/repository allowlist entries for tenant {tenant}")
        if missing:
            raise RuntimeError("Production API configuration is incomplete: " + ", ".join(missing))

    def validate_worker(self) -> None:
        self.validate_api()
        if self.environment != "production":
            return
        required = {
            "RUSA_AUDIT_HMAC_KEY": self.audit_hmac_key,
            "RUSA_WORKSPACE_ROOT": self.workspace_root,
            "RUSA_GIT_MIRROR_ROOT": self.git_mirror_root,
            "RUSA_GITHUB_TOKEN": self.github_token,
        }
        missing = [name for name, value in required.items() if not value]
        if self.audit_hmac_key and len(self.audit_hmac_key.encode("utf-8")) < 32:
            missing.append("RUSA_AUDIT_HMAC_KEY of at least 32 UTF-8 bytes")
        if self.workspace_root and not Path(self.workspace_root).is_absolute():
            missing.append("RUSA_WORKSPACE_ROOT must be an absolute path")
        if self.git_mirror_root and not Path(self.git_mirror_root).is_absolute():
            missing.append("RUSA_GIT_MIRROR_ROOT must be an absolute path")
        if not self.sandbox_image or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/:@-]+@sha256:[0-9a-f]{64}",
            self.sandbox_image,
        ):
            missing.append("RUSA_SANDBOX_IMAGE pinned by a valid immutable sha256 digest")
        if missing:
            raise RuntimeError("Production worker configuration is incomplete: " + ", ".join(missing))

    def validate(self) -> None:
        """Backward-compatible alias for validating the complete worker runtime."""
        self.validate_worker()
