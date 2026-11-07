"""Short-lived, single-repository GitHub App installation credentials."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


class GitHubAppTokenProvider:
    """Mints and caches installation tokens scoped to one tenant's one allowed repository."""

    API_URL = "https://api.github.com"
    TOKEN_SAFETY_SECONDS = 90
    TOKEN_PERMISSIONS = {
        "contents": "write",
        "pull_requests": "write",
        "checks": "read",
        "metadata": "read",
    }

    def __init__(
        self,
        app_id: int,
        private_key: str,
        tenant_installations: dict[str, int],
        tenant_repositories: dict[str, list[str]],
        *,
        timeout_seconds: float = 20.0,
    ) -> None:
        if type(app_id) is not int or app_id <= 0:
            raise ValueError("GitHub App ID must be a positive integer")
        if not isinstance(private_key, str) or "PRIVATE KEY-----" not in private_key:
            raise ValueError("GitHub App private key must be PEM encoded")
        if not isinstance(tenant_installations, dict) or not tenant_installations:
            raise ValueError("Tenant GitHub installation mapping is required")
        if not isinstance(tenant_repositories, dict) or not tenant_repositories:
            raise ValueError("Tenant repository allowlist is required")
        if set(tenant_installations) != set(tenant_repositories):
            raise ValueError("Every configured repository tenant must map to one GitHub App installation")
        if timeout_seconds <= 0:
            raise ValueError("GitHub App token request timeout must be positive")

        repository_owners: dict[str, str] = {}
        for tenant, installation_id in tenant_installations.items():
            if (
                not isinstance(tenant, str)
                or not tenant.strip()
                or type(installation_id) is not int
                or installation_id <= 0
            ):
                raise ValueError("GitHub installation mapping contains an invalid tenant or installation ID")
            repositories = tenant_repositories.get(tenant)
            if not isinstance(repositories, list) or not repositories:
                raise ValueError(f"Tenant {tenant!r} has no authorized repositories")
            for repository in repositories:
                if (
                    not isinstance(repository, str)
                    or repository.count("/") != 1
                    or any(not part for part in repository.split("/", 1))
                ):
                    raise ValueError(f"Tenant {tenant!r} has an invalid owner/repository allowlist")
                normalized = repository.lower()
                previous_tenant = repository_owners.get(normalized)
                if previous_tenant is not None and previous_tenant != tenant:
                    raise ValueError(
                        f"Repository {repository!r} cannot be assigned to multiple tenants"
                    )
                repository_owners[normalized] = tenant
        if len(set(tenant_installations.values())) != len(tenant_installations):
            raise ValueError("Each tenant must use a distinct GitHub App installation")

        self.app_id = app_id
        self.private_key = private_key
        self.tenant_installations = dict(tenant_installations)
        self.tenant_repositories = {
            tenant: tuple(repository.lower() for repository in repositories)
            for tenant, repositories in tenant_repositories.items()
        }
        self.timeout_seconds = timeout_seconds
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_env(
        cls,
        tenant_repositories: dict[str, list[str]],
    ) -> "GitHubAppTokenProvider":
        raw_app_id = os.getenv("RUSA_GITHUB_APP_ID", "")
        try:
            app_id = int(raw_app_id)
        except (TypeError, ValueError):
            raise RuntimeError("RUSA_GITHUB_APP_ID must be a positive integer") from None
        key_file = os.getenv("RUSA_GITHUB_APP_PRIVATE_KEY_FILE")
        if not key_file:
            raise RuntimeError("RUSA_GITHUB_APP_PRIVATE_KEY_FILE is required")
        try:
            private_key_path = Path(key_file).resolve(strict=True)
            if not private_key_path.is_file():
                raise OSError("not a regular file")
            if stat.S_IMODE(private_key_path.stat().st_mode) & 0o077:
                raise OSError("private key permissions are too broad")
            private_key = private_key_path.read_text(encoding="utf-8")
        except OSError:
            raise RuntimeError("GitHub App private key file is unavailable or has unsafe permissions") from None
        try:
            installations = json.loads(
                os.getenv("RUSA_TENANT_GITHUB_INSTALLATIONS_JSON", "{}")
            )
        except json.JSONDecodeError:
            raise RuntimeError("RUSA_TENANT_GITHUB_INSTALLATIONS_JSON must be valid JSON") from None
        if not isinstance(installations, dict):
            raise RuntimeError("RUSA_TENANT_GITHUB_INSTALLATIONS_JSON must be a JSON object")
        return cls(app_id, private_key, installations, tenant_repositories)

    def _app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError("Install rusa-ai[github-app] to use GitHub App authentication") from exc
        now = int(time.time())
        encoded = jwt.encode(
            {"iat": now - 30, "exp": now + 540, "iss": str(self.app_id)},
            self.private_key,
            algorithm="RS256",
        )
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("GitHub App JWT generation failed")
        return encoded

    @staticmethod
    def _parse_expiry(value: Any) -> float:
        if not isinstance(value, str):
            raise RuntimeError("GitHub returned an installation token without an expiry")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise RuntimeError("GitHub returned an invalid installation token expiry") from None

    def token_for(self, tenant_id: str, repository: str) -> str:
        if not isinstance(tenant_id, str) or tenant_id not in self.tenant_installations:
            raise PermissionError("Tenant has no configured GitHub App installation")
        if (
            not isinstance(repository, str)
            or repository.lower() not in self.tenant_repositories[tenant_id]
        ):
            raise PermissionError("Tenant is not authorized for this GitHub repository")
        cache_key = (tenant_id, repository.lower())
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[1] > now + self.TOKEN_SAFETY_SECONDS:
                return cached[0]

            installation_id = self.tenant_installations[tenant_id]
            app_jwt = self._app_jwt()
            try:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = client.post(
                        f"{self.API_URL}/app/installations/{installation_id}/access_tokens",
                        headers={
                            "Accept": "application/vnd.github+json",
                            "Authorization": f"Bearer {app_jwt}",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        json={
                            "repositories": [repository.split("/", 1)[1]],
                            "permissions": self.TOKEN_PERMISSIONS,
                        },
                    )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                raise RuntimeError("GitHub App installation token request failed") from None
            if not isinstance(payload, dict):
                raise RuntimeError("GitHub returned an invalid installation token response")
            token = payload.get("token")
            expiry = self._parse_expiry(payload.get("expires_at"))
            if (
                not isinstance(token, str)
                or len(token) < 20
                or expiry <= time.time() + self.TOKEN_SAFETY_SECONDS
            ):
                raise RuntimeError("GitHub returned an invalid or nearly expired installation token")
            self._cache[cache_key] = (token, expiry)
            return token

    def clear_cache(self) -> None:
        """Drop in-process token references; this does not revoke already-issued GitHub tokens."""
        with self._lock:
            self._cache.clear()
