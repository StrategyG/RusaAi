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
    api_database_url: str | None
    worker_database_url: str | None
    api_database_url_explicit: bool
    worker_database_url_explicit: bool
    audit_hmac_key: str | None
    api_keys: dict[str, str]
    metrics_api_key: str | None
    workspace_root: str | None
    sandbox_image: str | None
    git_mirror_root: str | None
    github_token: str | None
    model_base_url: str | None
    model_name: str
    tenant_repositories: dict[str, list[str]]
    aicode_enabled: bool = False

    @staticmethod
    def _looks_placeholder(value: str | None) -> bool:
        if value is None:
            return False
        lowered = value.strip().lower()
        if not lowered:
            return False
        markers = (
            "replace-with", "placeholder", "redacted", "long_random",
            "at_least_32", "fine_grained_repository_token", "registry_digest",
            "api_image_digest", "worker_image_digest", "model_host", "db_host",
            "managed-secret",
        )
        return any(marker in lowered for marker in markers)

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        environment = os.getenv("ENVIRONMENT", "development").lower()
        raw_enabled = os.getenv("RUSA_AICODE_ENABLED")
        if raw_enabled is None:
            aicode_enabled = environment != "production"
        elif raw_enabled.strip().lower() in {"1", "true", "yes", "on"}:
            aicode_enabled = True
        elif raw_enabled.strip().lower() in {"0", "false", "no", "off"}:
            aicode_enabled = False
        else:
            raise ValueError("RUSA_AICODE_ENABLED must be a boolean value")

        try:
            keys = json.loads(os.getenv("RUSA_API_KEYS", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("RUSA_API_KEYS must be a JSON object mapping API keys to tenant IDs") from exc
        if not isinstance(keys, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in keys.items()
        ):
            raise ValueError("RUSA_API_KEYS must map string API keys to string tenant IDs")

        try:
            tenant_repositories = json.loads(os.getenv("RUSA_TENANT_REPOSITORIES", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("RUSA_TENANT_REPOSITORIES must be a JSON object") from exc
        if not isinstance(tenant_repositories, dict) or any(
            not isinstance(tenant, str)
            or not isinstance(repositories, list)
            or any(not isinstance(repository, str) for repository in repositories)
            for tenant, repositories in tenant_repositories.items()
        ):
            raise ValueError("RUSA_TENANT_REPOSITORIES must map tenant IDs to repository-name arrays")

        legacy = os.getenv("RUSA_API_KEY")
        if legacy:
            keys.setdefault(legacy, os.getenv("RUSA_DEFAULT_TENANT", "default"))

        shared_dsn = os.getenv("RUSA_DATABASE_URL")
        api_dsn = os.getenv("RUSA_API_DATABASE_URL")
        worker_dsn = os.getenv("RUSA_WORKER_DATABASE_URL")
        return cls(
            environment=environment,
            database_url=shared_dsn,
            api_database_url=api_dsn or shared_dsn,
            worker_database_url=worker_dsn or shared_dsn,
            api_database_url_explicit=bool(api_dsn),
            worker_database_url_explicit=bool(worker_dsn),
            audit_hmac_key=os.getenv("RUSA_AUDIT_HMAC_KEY"),
            api_keys=keys,
            metrics_api_key=os.getenv("RUSA_METRICS_API_KEY"),
            workspace_root=os.getenv("RUSA_WORKSPACE_ROOT"),
            sandbox_image=os.getenv("RUSA_SANDBOX_IMAGE"),
            git_mirror_root=os.getenv("RUSA_GIT_MIRROR_ROOT"),
            github_token=os.getenv("RUSA_GITHUB_TOKEN"),
            model_base_url=os.getenv("RUSA_MODEL_BASE_URL"),
            model_name=os.getenv("RUSA_MODEL_NAME", "rusa-ai"),
            tenant_repositories=tenant_repositories,
            aicode_enabled=aicode_enabled,
        )

    @classmethod
    def _validate_database_url(
        cls, value: str | None, variable: str, expected_user: str
    ) -> list[str]:
        errors: list[str] = []
        if not value:
            return [f"{variable} is required"]
        try:
            parsed = urlparse(value)
            hostname = parsed.hostname
            username = parsed.username
            password = parsed.password
        except ValueError:
            return [f"{variable} is not a valid PostgreSQL URL"]
        if parsed.scheme not in {"postgres", "postgresql"}:
            errors.append(f"{variable} must use PostgreSQL")
        if not hostname or not parsed.path.strip("/"):
            errors.append(f"{variable} must include a database host and database name")
        if username != expected_user:
            errors.append(f"{variable} must authenticate as {expected_user}")
        if cls._looks_placeholder(hostname) or cls._looks_placeholder(password):
            errors.append(f"{variable} must not contain example host/password placeholders")
        ssl_modes = parse_qs(parsed.query).get("sslmode", [])
        if not ssl_modes or ssl_modes[-1] != "verify-full":
            errors.append(f"{variable} must use sslmode=verify-full")
        return errors

    def _validate_tenants_and_model(self) -> list[str]:
        errors: list[str] = []
        if not self.tenant_repositories:
            errors.append("RUSA_TENANT_REPOSITORIES is required")
        if any(not tenant.strip() for tenant in self.tenant_repositories):
            errors.append("RUSA_TENANT_REPOSITORIES contains an empty tenant identifier")
        for tenant, repositories in self.tenant_repositories.items():
            if not repositories:
                errors.append(f"tenant {tenant} must have at least one authorized repository")
            if any(
                not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                for repository in repositories
            ):
                errors.append(f"tenant {tenant} has an invalid owner/repository allowlist entry")
        if not self.model_base_url:
            errors.append("RUSA_MODEL_BASE_URL is required")
        else:
            model_url = urlparse(self.model_base_url)
            if (
                model_url.scheme != "https"
                or not model_url.hostname
                or model_url.username
                or model_url.password
            ):
                errors.append("RUSA_MODEL_BASE_URL must use credential-free HTTPS")
            if self._looks_placeholder(model_url.hostname):
                errors.append("RUSA_MODEL_BASE_URL must point to a real model host")
        return errors

    def validate_api(self) -> None:
        if self.environment != "production":
            return
        missing: list[str] = []
        if not self.api_database_url_explicit:
            missing.append("RUSA_API_DATABASE_URL (legacy shared DSN is not allowed in production)")
        missing.extend(
            self._validate_database_url(
                self.api_database_url, "RUSA_API_DATABASE_URL", "rusa_api_login"
            )
        )
        if not self.api_keys:
            missing.append("RUSA_API_KEYS")
        if any(len(key.encode("utf-8")) < 32 for key in self.api_keys):
            missing.append("API keys of at least 32 UTF-8 bytes")
        if any(self._looks_placeholder(key) for key in self.api_keys):
            missing.append("API keys must not contain example placeholders")
        if any(not tenant.strip() for tenant in self.api_keys.values()):
            missing.append("non-empty API-key tenant mappings")
        unmapped_tenants = set(self.api_keys.values()) - set(self.tenant_repositories)
        if unmapped_tenants:
            missing.append("every API-key tenant must have a repository allowlist")
        if not self.metrics_api_key:
            missing.append("RUSA_METRICS_API_KEY")
        elif len(self.metrics_api_key.encode("utf-8")) < 32 or self._looks_placeholder(self.metrics_api_key):
            missing.append("RUSA_METRICS_API_KEY must be a real secret of at least 32 UTF-8 bytes")
        missing.extend(self._validate_tenants_and_model())
        if missing:
            raise RuntimeError("Production API configuration is incomplete: " + ", ".join(missing))

    def validate_worker(self) -> None:
        if self.environment != "production":
            return
        missing: list[str] = []
        if not self.worker_database_url_explicit:
            missing.append("RUSA_WORKER_DATABASE_URL (legacy shared DSN is not allowed in production)")
        missing.extend(
            self._validate_database_url(
                self.worker_database_url, "RUSA_WORKER_DATABASE_URL", "rusa_worker_login"
            )
        )
        required = {
            "RUSA_AUDIT_HMAC_KEY": self.audit_hmac_key,
            "RUSA_WORKSPACE_ROOT": self.workspace_root,
            "RUSA_GIT_MIRROR_ROOT": self.git_mirror_root,
            "RUSA_GITHUB_TOKEN": self.github_token,
            "RUSA_SANDBOX_IMAGE": self.sandbox_image,
        }
        missing.extend(name for name, value in required.items() if not value)
        if self.audit_hmac_key and (
            len(self.audit_hmac_key.encode("utf-8")) < 32
            or self._looks_placeholder(self.audit_hmac_key)
        ):
            missing.append("RUSA_AUDIT_HMAC_KEY must be a real secret of at least 32 UTF-8 bytes")
        if self.github_token and (
            len(self.github_token.encode("utf-8")) < 20 or self._looks_placeholder(self.github_token)
        ):
            missing.append("RUSA_GITHUB_TOKEN must be a real fine-grained token, not a placeholder")
        if self.workspace_root and not Path(self.workspace_root).is_absolute():
            missing.append("RUSA_WORKSPACE_ROOT must be an absolute path")
        if self.git_mirror_root and not Path(self.git_mirror_root).is_absolute():
            missing.append("RUSA_GIT_MIRROR_ROOT must be an absolute path")
        if self.sandbox_image and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/:@-]+@sha256:[0-9a-f]{64}",
            self.sandbox_image,
        ):
            missing.append("RUSA_SANDBOX_IMAGE must be pinned by a valid immutable sha256 digest")
        missing.extend(self._validate_tenants_and_model())
        if missing:
            raise RuntimeError("Production worker configuration is incomplete: " + ", ".join(missing))

    def validate(self) -> None:
        """Validate both service configurations for tests and deployment preflight."""
        self.validate_api()
        self.validate_worker()
