"""Constrained GitHub repository tool gateway.

The model never receives a shell or raw HTTP client. Every operation is
validated against the task repository, work branch, worker role, and policy.
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any
from urllib.parse import quote

import httpx

from .contracts import CodingTask, ToolEvidence, WorkerRole
from .policy import AutonomyPolicy, RiskLevel


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubRepositoryGateway:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 30,
    ) -> None:
        if not token or len(token) < 20:
            raise ValueError("a GitHub token with repository permissions is required")
        if not api_url.startswith("https://"):
            raise ValueError("GitHub API must use HTTPS")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = httpx.Timeout(connect=5, read=timeout_seconds, write=30, pool=5)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            raise GitHubAPIError(
                response.status_code,
                f"GitHub API request failed with HTTP {response.status_code}",
            )
        if not response.content:
            return {}
        return response.json()

    @staticmethod
    def validate_path(path: str) -> str:
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or len(path) > 240
            or any(ord(character) < 32 for character in path)
            or any(character in path for character in ("?", "#", ":"))
            or any(part in {"", ".", "..", ".git"} for part in path.split("/"))
        ):
            raise ValueError("repository path is invalid")
        return path

    async def get_branch(self, repository: str, branch: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/repos/{repository}/branches/{quote(branch, safe='')}",
        )

    async def list_tree(self, repository: str, ref: str) -> dict[str, Any]:
        branch = await self.get_branch(repository, ref)
        sha = branch["commit"]["sha"]
        tree = await self._request(
            "GET",
            f"/repos/{repository}/git/trees/{sha}",
            params={"recursive": "1"},
        )
        return {
            "sha": sha,
            "truncated": bool(tree.get("truncated")),
            "paths": [
                item["path"]
                for item in tree.get("tree", [])
                if item.get("type") == "blob"
            ],
        }

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        safe_path = self.validate_path(path)
        item = await self._request(
            "GET",
            f"/repos/{repository}/contents/{quote(safe_path, safe='/')}",
            params={"ref": ref},
        )
        if item.get("type") != "file" or not item.get("content"):
            raise RuntimeError("requested repository item is not a readable text file")
        try:
            return base64.b64decode(item["content"], validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("repository file is not valid UTF-8 text") from exc

    async def create_branch(
        self,
        repository: str,
        base_branch: str,
        work_branch: str,
    ) -> dict[str, Any]:
        base = await self.get_branch(repository, base_branch)
        try:
            return await self._request(
                "POST",
                f"/repos/{repository}/git/refs",
                json_body={
                    "ref": f"refs/heads/{work_branch}",
                    "sha": base["commit"]["sha"],
                },
            )
        except GitHubAPIError as exc:
            if exc.status_code != 422:
                raise
            existing = await self.get_branch(repository, work_branch)
            return {"ref": f"refs/heads/{work_branch}", "object": existing["commit"]}

    async def write_file(
        self,
        repository: str,
        path: str,
        content: str,
        branch: str,
        message: str,
    ) -> dict[str, Any]:
        safe_path = self.validate_path(path)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        file_path = f"/repos/{repository}/contents/{quote(safe_path, safe='/')}"
        current_sha = None
        try:
            current = await self._request("GET", file_path, params={"ref": branch})
            current_sha = current.get("sha")
            if current.get("encoding") == "base64":
                old_content = base64.b64decode(current.get("content", "")).decode("utf-8")
                if old_content == content:
                    return {"unchanged": True, "path": safe_path, "branch": branch}
        except GitHubAPIError as exc:
            if exc.status_code != 404:
                raise
        body: dict[str, Any] = {
            "message": message[:200],
            "content": encoded,
            "branch": branch,
        }
        if current_sha:
            body["sha"] = current_sha
        result = await self._request("PUT", file_path, json_body=body)
        return {
            "unchanged": False,
            "path": safe_path,
            "branch": branch,
            "commit_sha": result.get("commit", {}).get("sha", ""),
            "content_sha": result.get("content", {}).get("sha", ""),
        }

    async def create_pull_request(
        self,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/repos/{repository}/pulls",
            json_body={
                "title": title[:200],
                "body": body[:60000],
                "head": head,
                "base": base,
                "draft": True,
                "maintainer_can_modify": True,
            },
        )

    async def get_check_runs(self, repository: str, branch: str) -> tuple[str, list[dict[str, Any]]]:
        branch_data = await self.get_branch(repository, branch)
        sha = branch_data["commit"]["sha"]
        result = await self._request(
            "GET",
            f"/repos/{repository}/commits/{sha}/check-runs",
            params={"per_page": 100},
        )
        return sha, result.get("check_runs", [])


class GitHubToolGateway:
    ROLE_ACTIONS = {
        WorkerRole.ANALYST: {"repo.list_tree", "repo.read_file"},
        WorkerRole.ARCHITECT: {"repo.list_tree", "repo.read_file"},
        WorkerRole.DEVELOPER: {
            "repo.list_tree",
            "repo.read_file",
            "repo.create_branch",
            "repo.write_file",
            "repo.create_pull_request",
        },
        WorkerRole.DEBUGGER: {"repo.list_tree", "repo.read_file", "ci.wait_for_checks"},
        WorkerRole.TESTER: {"ci.wait_for_checks"},
        WorkerRole.SECURITY: {"repo.list_tree", "repo.read_file", "ci.wait_for_checks"},
    }
    POLICY_ACTIONS = {
        "repo.list_tree": "repo.read",
        "repo.read_file": "repo.read",
        "repo.create_branch": "repo.branch.create",
        "repo.write_file": "repo.file.write",
        "repo.create_pull_request": "repo.pull_request.create",
        "ci.wait_for_checks": "ci.status.read",
    }
    PROTECTED_WRITE_PATHS = (
        ".github/",
        ".env",
        "Dockerfile",
        "docker-compose.yml",
        "requirements",
        "pyproject.toml",
        "CODEOWNERS",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
    )
    SENSITIVE_NAMES = frozenset(
        {
            ".npmrc",
            ".pypirc",
            ".netrc",
            "id_rsa",
            "id_ed25519",
            "credentials",
            "credentials.json",
            "secrets.json",
        }
    )
    SENSITIVE_SUFFIXES = (
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".tfstate",
        ".tfstate.backup",
    )

    @classmethod
    def is_sensitive_path(cls, path: str) -> bool:
        normalized = path.lower()
        parts = normalized.split("/")
        basename = parts[-1]
        return (
            normalized == ".env"
            or normalized.startswith(".env.")
            or basename in cls.SENSITIVE_NAMES
            or any(part in {".ssh", ".aws", ".gnupg", "secrets", "secret"} for part in parts)
            or normalized.endswith(cls.SENSITIVE_SUFFIXES)
        )

    @classmethod
    def is_protected_write_path(cls, path: str) -> bool:
        normalized = path.lower()
        return (
            normalized == ".github"
            or any(
                normalized == protected.lower()
                or normalized.startswith(protected.lower())
                for protected in cls.PROTECTED_WRITE_PATHS
            )
            or cls.is_sensitive_path(normalized)
        )

    @staticmethod
    def redact_secrets(content: str) -> str:
        patterns = (
            (
                re.compile(
                    r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret)\b"
                    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
                ),
                r"\1\2[REDACTED]",
            ),
            (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
            (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
            (
                re.compile(
                    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
                    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                    re.DOTALL,
                ),
                "[REDACTED_PRIVATE_KEY]",
            ),
            (
                re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
                "Bearer [REDACTED]",
            ),
        )
        for pattern, replacement in patterns:
            content = pattern.sub(replacement, content)
        return content

    def __init__(
        self,
        github: GitHubRepositoryGateway,
        policy: AutonomyPolicy | None = None,
        max_file_bytes: int = 262144,
        allowed_repositories: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.github = github
        self.policy = policy or AutonomyPolicy()
        self.max_file_bytes = max_file_bytes
        self.allowed_repositories = frozenset(
            item.strip().lower()
            for item in (allowed_repositories or set())
            if item.strip()
        )
        if not self.allowed_repositories:
            raise ValueError("an explicit GitHub repository allowlist is required")

    async def execute(
        self,
        action: str,
        arguments: dict[str, Any],
        task: CodingTask,
        role: WorkerRole,
    ) -> dict[str, Any]:
        if task.repository.full_name.lower() not in self.allowed_repositories:
            return {"ok": False, "error": "repository is not in the configured GitHub allowlist"}
        if action not in self.ROLE_ACTIONS.get(role, set()):
            return {"ok": False, "error": f"{role.value} is not allowed to use {action}"}
        policy_action = self.POLICY_ACTIONS.get(action)
        if not policy_action:
            return {"ok": False, "error": "unknown tool action"}
        decision = self.policy.evaluate(policy_action, RiskLevel.LOW)
        if not decision.allowed:
            return {
                "ok": False,
                "error": decision.reason,
                "requires_approval": decision.requires_approval,
            }

        repository = task.repository.full_name
        base = task.repository.base_branch
        work = task.repository.work_branch
        try:
            if action == "repo.list_tree":
                ref = arguments.get("ref", base)
                if ref not in {base, work}:
                    raise ValueError("only the task base and work branches may be inspected")
                data = await self.github.list_tree(repository, ref)
                return {"ok": True, "data": data}
            if action == "repo.read_file":
                ref = arguments.get("ref", base)
                if ref not in {base, work}:
                    raise ValueError("only the task base and work branches may be inspected")
                path = self.github.validate_path(arguments["path"])
                if self.is_sensitive_path(path):
                    raise PermissionError("sensitive repository files cannot be sent to model workers")
                data = await self.github.read_file(repository, path, ref)
                return {"ok": True, "data": self.redact_secrets(data)}
            if action == "repo.create_branch":
                data = await self.github.create_branch(repository, base, work)
                return {"ok": True, "data": data}
            if action == "repo.write_file":
                path = self.github.validate_path(arguments["path"])
                if self.is_protected_write_path(path):
                    raise PermissionError("this path is protected and requires human review")
                content = arguments["content"]
                if not isinstance(content, str) or len(content.encode("utf-8")) > self.max_file_bytes:
                    raise ValueError("file content exceeds the configured size limit")
                data = await self.github.write_file(
                    repository,
                    path,
                    content,
                    work,
                    str(arguments.get("message", "AICode: update repository file")),
                )
                artifacts = [data["path"]] if not data.get("unchanged") else []
                return {"ok": True, "data": data, "artifacts": artifacts}
            if action == "repo.create_pull_request":
                data = await self.github.create_pull_request(
                    repository,
                    work,
                    base,
                    str(arguments["title"]),
                    str(arguments.get("body", "")),
                )
                return {
                    "ok": True,
                    "data": {
                        "number": data.get("number"),
                        "html_url": data.get("html_url"),
                        "draft": data.get("draft", True),
                    },
                    "artifacts": [data.get("html_url", "")],
                }
            if action == "ci.wait_for_checks":
                wait_seconds = int(arguments.get("wait_seconds", 300))
                if not 0 <= wait_seconds <= 900:
                    raise ValueError("wait_seconds must be between 0 and 900")
                deadline = asyncio.get_running_loop().time() + wait_seconds
                sha = ""
                checks: list[dict[str, Any]] = []
                while True:
                    sha, checks = await self.github.get_check_runs(repository, work)
                    if checks and all(item.get("status") == "completed" for item in checks):
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(5)
                evidence = [
                    ToolEvidence(
                        kind="ci_check",
                        name=str(item.get("name", "unnamed-check")),
                        status=str(item.get("conclusion") or item.get("status") or "unknown"),
                        source=str(item.get("html_url", "")),
                        sha=sha,
                    ).__dict__
                    for item in checks
                ]
                return {
                    "ok": bool(checks),
                    "data": {
                        "sha": sha,
                        "checks": [
                            {
                                "name": item.get("name"),
                                "status": item.get("status"),
                                "conclusion": item.get("conclusion"),
                            }
                            for item in checks
                        ],
                    },
                    "evidence": evidence,
                    "error": None if checks else "no CI check runs were found for the work branch",
                }
        except (KeyError, TypeError, ValueError, PermissionError, GitHubAPIError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "tool action was not implemented"}
