"""Policy-enforced GitHub repository gateway.

Writes are restricted to allowlisted repositories and task work branches.
Protected build, workflow, deployment, and secret files require human approval.
Secret-bearing files cannot be read. There is no merge or deployment method.
"""
from __future__ import annotations

import base64
import binascii
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .contracts import RepositoryTarget
from .policy import AutonomyPolicy, RiskLevel


class GatewayError(RuntimeError):
    """Base class for repository gateway failures."""


class ApprovalRequired(GatewayError):
    """Raised when an operation requires explicit human approval."""


class AuthorizationDenied(GatewayError):
    """Raised when an operation is not allowed by policy."""


class GitHubGateway:
    MAX_FILE_BYTES = 1_000_000
    PROTECTED_BASENAMES = frozenset(
        {
            "dockerfile",
            "docker-compose.yml",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "pyproject.toml",
            "poetry.lock",
            "uv.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        }
    )
    PROTECTED_PREFIXES = (
        ".github/workflows/",
        ".github/actions/",
        "deploy/",
        "deployment/",
        "infra/",
        "terraform/",
        "k8s/",
        "secrets/",
    )
    SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".secret")

    def __init__(
        self,
        token: str,
        allowed_repositories: set[str],
        policy: AutonomyPolicy | None = None,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if len(token.strip()) < 20:
            raise ValueError("a GitHub token with repository access is required")
        repositories = {item.strip().lower() for item in allowed_repositories}
        if not repositories:
            raise ValueError("at least one repository must be explicitly allowlisted")
        for repository in repositories:
            parts = repository.split("/")
            if (
                len(parts) != 2
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part) for part in parts)
            ):
                raise ValueError(f"invalid repository allowlist entry: {repository!r}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        parsed_url = urlsplit(api_base_url)
        local_http = parsed_url.scheme == "http" and parsed_url.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if parsed_url.scheme != "https" and not local_http:
            raise ValueError("GitHub API base URL must use HTTPS outside local tests")
        if not parsed_url.netloc or parsed_url.username or parsed_url.password:
            raise ValueError("GitHub API base URL is invalid")
        self._token = token
        self._allowed_repositories = frozenset(repositories)
        self._policy = policy or AutonomyPolicy()
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    def _authorize(self, action: str, risk: RiskLevel = RiskLevel.LOW) -> None:
        decision = self._policy.evaluate(action, risk)
        if decision.requires_approval:
            raise ApprovalRequired(decision.reason)
        if not decision.allowed:
            raise AuthorizationDenied(decision.reason)

    def _validate_target(self, target: RepositoryTarget) -> str:
        repository = target.full_name.lower()
        if repository not in self._allowed_repositories:
            raise AuthorizationDenied("repository is not in the explicit allowlist")
        if target.work_branch == target.base_branch:
            raise AuthorizationDenied("writes to the base branch are prohibited")
        if not target.work_branch.startswith(("feature/", "fix/", "chore/", "docs/")):
            raise AuthorizationDenied("work branch does not use an approved prefix")
        return target.full_name

    @staticmethod
    def _validate_path(path: str) -> str:
        if not path or len(path) > 512 or "\\" in path or "\x00" in path:
            raise ValueError("invalid repository file path")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or any(part in {"", ".", "..", ".git"} for part in parsed.parts):
            raise ValueError("repository path traversal is prohibited")
        normalized = parsed.as_posix()
        if normalized.startswith(".git/"):
            raise ValueError("Git metadata cannot be accessed")
        return normalized

    @classmethod
    def _is_secret_path(cls, path: str) -> bool:
        lowered = path.lower()
        basename = lowered.rsplit("/", 1)[-1]
        return (
            lowered.startswith("secrets/")
            or basename == ".env"
            or basename.startswith(".env.")
            or basename.endswith(cls.SECRET_SUFFIXES)
        )

    @classmethod
    def _is_protected_path(cls, path: str) -> bool:
        lowered = path.lower()
        basename = lowered.rsplit("/", 1)[-1]
        return (
            basename in cls.PROTECTED_BASENAMES
            or lowered.startswith(cls.PROTECTED_PREFIXES)
            or cls._is_secret_path(path)
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            base_url=self._api_base_url,
            headers=headers,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        ) as client:
            return await client.request(method, path, params=params, json=payload)

    @staticmethod
    def _repo_path(repository: str) -> str:
        owner, name = repository.split("/", 1)
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    @staticmethod
    def _ref_path(branch: str) -> str:
        return quote(branch, safe="")

    async def create_work_branch(self, target: RepositoryTarget) -> dict[str, Any]:
        repository = self._validate_target(target)
        self._authorize("branch_create")
        repo_path = self._repo_path(repository)

        existing = await self._request(
            "GET",
            f"{repo_path}/git/ref/heads/{self._ref_path(target.work_branch)}",
        )
        if existing.status_code == 200:
            return {"created": False, "ref": existing.json()}
        if existing.status_code != 404:
            existing.raise_for_status()

        base = await self._request(
            "GET",
            f"{repo_path}/git/ref/heads/{self._ref_path(target.base_branch)}",
        )
        base.raise_for_status()
        base_sha = base.json().get("object", {}).get("sha")
        if not isinstance(base_sha, str) or len(base_sha) != 40:
            raise GatewayError("GitHub returned an invalid base commit SHA")

        created = await self._request(
            "POST",
            f"{repo_path}/git/refs",
            payload={"ref": f"refs/heads/{target.work_branch}", "sha": base_sha},
        )
        if created.status_code == 422:
            existing = await self._request(
                "GET",
                f"{repo_path}/git/ref/heads/{self._ref_path(target.work_branch)}",
            )
            existing.raise_for_status()
            return {"created": False, "ref": existing.json()}
        created.raise_for_status()
        return {"created": True, "ref": created.json()}

    async def read_file(
        self,
        target: RepositoryTarget,
        path: str,
        branch: str | None = None,
    ) -> bytes:
        repository = self._validate_target(target)
        self._authorize("repo_read")
        safe_path = self._validate_path(path)
        if self._is_secret_path(safe_path):
            raise AuthorizationDenied("reading secret-bearing repository paths is prohibited")
        selected_branch = branch or target.work_branch
        if selected_branch not in {target.work_branch, target.base_branch}:
            raise AuthorizationDenied("only the task base or work branch may be read")

        response = await self._request(
            "GET",
            f"{self._repo_path(repository)}/contents/{quote(safe_path, safe='/')}",
            params={"ref": selected_branch},
        )
        if response.status_code == 404:
            raise FileNotFoundError(safe_path)
        response.raise_for_status()
        data = response.json()
        if data.get("type") != "file" or data.get("encoding") != "base64":
            raise GatewayError("requested path is not a supported regular file")
        try:
            content = base64.b64decode(data.get("content", ""), validate=False)
        except (ValueError, binascii.Error) as exc:
            raise GatewayError("GitHub returned invalid base64 file content") from exc
        if len(content) > self.MAX_FILE_BYTES:
            raise GatewayError("repository file exceeds the configured size limit")
        return content

    async def write_file(
        self,
        target: RepositoryTarget,
        path: str,
        content: bytes,
        commit_message: str,
    ) -> dict[str, Any]:
        repository = self._validate_target(target)
        safe_path = self._validate_path(path)
        if self._is_protected_path(safe_path):
            self._authorize("permission_change", RiskLevel.HIGH)
        self._authorize("workspace_write", RiskLevel.MEDIUM)

        if not isinstance(content, bytes) or len(content) > self.MAX_FILE_BYTES:
            raise ValueError("file content must be bytes no larger than 1 MB")
        if not commit_message.strip() or len(commit_message) > 200:
            raise ValueError("commit message must contain 1-200 characters")

        await self.create_work_branch(target)
        repo_path = self._repo_path(repository)
        existing = await self._request(
            "GET",
            f"{repo_path}/contents/{quote(safe_path, safe='/')}",
            params={"ref": target.work_branch},
        )
        if existing.status_code not in {200, 404}:
            existing.raise_for_status()

        payload: dict[str, Any] = {
            "message": commit_message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": target.work_branch,
        }
        if existing.status_code == 200:
            file_sha = existing.json().get("sha")
            if not isinstance(file_sha, str) or len(file_sha) != 40:
                raise GatewayError("GitHub returned an invalid file SHA")
            payload["sha"] = file_sha

        response = await self._request(
            "PUT",
            f"{repo_path}/contents/{quote(safe_path, safe='/')}",
            payload=payload,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("commit", {}).get("sha") is None:
            raise GatewayError("GitHub did not confirm the created commit")
        return result

    async def create_pull_request(
        self,
        target: RepositoryTarget,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        repository = self._validate_target(target)
        self._authorize("pull_request_create")
        if not title.strip() or len(title) > 256:
            raise ValueError("pull request title must contain 1-256 characters")
        if len(body) > 60000:
            raise ValueError("pull request body exceeds 60000 characters")

        owner = repository.split("/", 1)[0]
        repo_path = self._repo_path(repository)
        existing = await self._request(
            "GET",
            f"{repo_path}/pulls",
            params={
                "state": "open",
                "head": f"{owner}:{target.work_branch}",
                "base": target.base_branch,
            },
        )
        existing.raise_for_status()
        open_prs = existing.json()
        if open_prs:
            return open_prs[0]

        response = await self._request(
            "POST",
            f"{repo_path}/pulls",
            payload={
                "title": title,
                "body": body,
                "head": target.work_branch,
                "base": target.base_branch,
                "maintainer_can_modify": False,
            },
        )
        response.raise_for_status()
        return response.json()
