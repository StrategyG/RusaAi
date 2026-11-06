"""Constrained GitHub repository tool gateway.

The model never receives a shell or raw HTTP client. Every operation is
validated against the task repository, work branch, worker role, and policy.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

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
        parsed = urlsplit(api_url)
        if not token or len(token) < 20:
            raise ValueError("a GitHub token with repository permissions is required")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.github.com"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError("GitHub API URL must be the official HTTPS api.github.com endpoint")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub timeout must be between 1 and 120 seconds")
        self.api_url = "https://api.github.com"
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
            trust_env=False,
        ) as client:
            async with client.stream(
                method,
                path,
                params=params,
                json=json_body,
            ) as response:
                if response.status_code >= 400:
                    raise GitHubAPIError(
                        response.status_code,
                        f"GitHub API request failed with HTTP {response.status_code}",
                    )
                chunks = []
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > 2_000_000:
                        raise GitHubAPIError(
                            502,
                            "GitHub API response exceeded the configured size limit",
                        )
                    chunks.append(chunk)
        if not chunks:
            return {}
        try:
            return json.loads(b"".join(chunks))
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(502, "GitHub API returned invalid JSON") from exc

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
        base_sha = base["commit"]["sha"]
        try:
            return await self._request(
                "POST",
                f"/repos/{repository}/git/refs",
                json_body={
                    "ref": f"refs/heads/{work_branch}",
                    "sha": base_sha,
                },
            )
        except GitHubAPIError as exc:
            if exc.status_code != 422:
                raise
            existing = await self.get_branch(repository, work_branch)
            if existing["commit"]["sha"] != base_sha:
                raise RuntimeError(
                    "work branch already exists with a different head; refusing to reuse it"
                ) from exc
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
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise RuntimeError("GitHub returned an invalid commit SHA")
        result = await self._request(
            "GET",
            f"/repos/{repository}/commits/{sha}/check-runs",
            params={"per_page": 100},
        )
        checks = result.get("check_runs", [])
        if result.get("total_count", len(checks)) > len(checks):
            raise RuntimeError("CI check pagination is incomplete; refusing partial evidence")
        return sha, checks


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
        ".circleci/",
        ".buildkite/",
        ".azure-pipelines/",
        ".devcontainer/",
        ".terraform/",
        "terraform/",
        "infra/",
        "infrastructure/",
        "k8s/",
        "kubernetes/",
        "helm/",
        "deploy/",
        ".env",
        "Dockerfile",
        "docker-compose.yml",
        "compose.yml",
        "compose.yaml",
        "requirements",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
    )
    PROTECTED_WRITE_NAMES = frozenset(
        {
            "codeowners",
            ".gitlab-ci.yml",
            "azure-pipelines.yml",
            "jenkinsfile",
            "dockerfile",
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
            "pyproject.toml",
            "poetry.lock",
            "uv.lock",
            "pipfile.lock",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-model.txt",
            "requirements.lock",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "cargo.toml",
            "cargo.lock",
            "go.mod",
            "go.sum",
            "gemfile",
            "gemfile.lock",
            "composer.json",
            "composer.lock",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "gradle.lockfile",
            "nuget.config",
            ".travis.yml",
            "appveyor.yml",
            "bitbucket-pipelines.yml",
            "jenkinsfile",
        }
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
            basename == ".env"
            or basename.startswith(".env.")
            or basename in cls.SENSITIVE_NAMES
            or basename in {"secret", "secrets", ".envrc", "kubeconfig"}
            or basename.startswith(("secret.", "secrets.", "credentials."))
            or any(
                part in {".ssh", ".aws", ".gnupg", ".kube", "secrets", "secret"}
                for part in parts
            )
            or normalized.endswith(cls.SENSITIVE_SUFFIXES)
        )

    @classmethod
    def is_protected_write_path(cls, path: str) -> bool:
        normalized = path.lower()
        basename = normalized.rsplit("/", 1)[-1]
        return (
            basename in cls.PROTECTED_WRITE_NAMES
            or normalized == ".github"
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
                    r"(?i)([A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret|token)[A-Za-z0-9_-]*)"
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
            (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [REDACTED]"),
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
        tenant_repositories: dict[str, set[str] | frozenset[str]] | None = None,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.github = github
        self.policy = policy or AutonomyPolicy()
        self.max_file_bytes = max_file_bytes
        self.allowed_repositories = frozenset(
            item.strip().lower()
            for item in (allowed_repositories or set())
            if item.strip()
        )
        self.tenant_repositories = None
        if tenant_repositories is not None:
            normalized: dict[str, frozenset[str]] = {}
            for tenant_id, repositories in tenant_repositories.items():
                if not isinstance(tenant_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9_.:-]{1,128}", tenant_id
                ):
                    raise ValueError("tenant repository allowlist contains an invalid tenant ID")
                repo_set = frozenset(
                    item.strip().lower()
                    for item in repositories
                    if isinstance(item, str) and item.strip()
                )
                if not repo_set:
                    raise ValueError(
                        f"tenant {tenant_id!r} must have at least one allowed repository"
                    )
                normalized[tenant_id] = repo_set
            if not normalized:
                raise ValueError("tenant repository allowlist must not be empty")
            self.tenant_repositories = normalized
        elif not self.allowed_repositories:
            raise ValueError("an explicit GitHub repository allowlist is required")

    async def execute(
        self,
        action: str,
        arguments: dict[str, Any],
        task: CodingTask,
        role: WorkerRole,
    ) -> dict[str, Any]:
        repository_name = task.repository.full_name.lower()
        if self.tenant_repositories is not None:
            tenant_allowed = self.tenant_repositories.get(task.tenant_id, frozenset())
            if repository_name not in tenant_allowed:
                return {
                    "ok": False,
                    "error": "repository is not allowlisted for the authenticated tenant",
                }
        elif repository_name not in self.allowed_repositories:
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
                if len(data.encode("utf-8")) > self.max_file_bytes:
                    raise ValueError("file content exceeds the configured size limit")
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
                url = str(data.get("html_url", ""))
                head_sha = str(data.get("head", {}).get("sha", ""))
                if data.get("draft") is not True or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
                    raise RuntimeError("GitHub did not confirm a draft PR with a full head SHA")
                evidence = ToolEvidence(
                    kind="pull_request",
                    name="draft_pull_request",
                    status="draft",
                    source=url,
                    sha=head_sha,
                )
                return {
                    "ok": True,
                    "data": {
                        "number": data.get("number"),
                        "html_url": url,
                        "draft": True,
                    },
                    "evidence": [evidence.__dict__],
                    "artifacts": [url],
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
                completed = bool(checks) and all(
                    item.get("status") == "completed" for item in checks
                )
                all_checks_succeeded = completed and all(
                    item.get("conclusion") == "success" for item in checks
                )
                failed_checks = [
                    str(item.get("name", "unnamed-check"))
                    for item in checks
                    if item.get("status") != "completed" or item.get("conclusion") != "success"
                ]
                return {
                    "ok": all_checks_succeeded,
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
                    "error": (
                        None
                        if all_checks_succeeded
                        else "CI checks are missing, pending, or unsuccessful: "
                        + ", ".join(failed_checks or ["no check runs"])
                    ),
                }
        except (KeyError, TypeError, ValueError, PermissionError, GitHubAPIError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "tool action was not implemented"}
