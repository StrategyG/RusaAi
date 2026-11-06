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
    git_mirror_root: str | None
    github_token: str | None
    model_base_url: str | None
    model_name: str
    tenant_repositories: dict[str, list[str]]

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
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
        if not self.sandbox_image or "@sha256:" not in self.sandbox_image:
            missing.append("RUSA_SANDBOX_IMAGE pinned by immutable sha256 digest")
        if missing:
            raise RuntimeError("Production worker configuration is incomplete: " + ", ".join(missing))

    def validate(self) -> None:
        """Backward-compatible alias for validating the complete worker runtime."""
        self.validate_worker()
