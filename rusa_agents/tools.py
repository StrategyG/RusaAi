"""Strict tool registry with bounded GitHub operations and CI evidence lookup."""
from __future__ import annotations

import base64
import os
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx
import jsonschema

from .security import redact_sensitive_data


class ToolError(RuntimeError):
    pass


class GitHubAPIError(ToolError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"GitHub API returned HTTP {status_code}")


def _safe_repo(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ToolError("Invalid repository identifier")
    return value


def _safe_branch(value: str) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]+", value)
        or value.startswith("/")
        or value.endswith("/")
        or ".." in value.split("/")
        or value in {".", ".."}
    ):
        raise ToolError("Unsafe Git reference")
    return value


def _assert_safe_file_path(value: str) -> None:
    parts = [part.lower() for part in value.split("/")]
    basename = parts[-1]
    safe_env_examples = {".env.example", ".env.sample", ".env.template"}
    if basename == ".env" or (basename.startswith(".env.") and basename not in safe_env_examples):
        raise ToolError("Reading or writing environment secret files is prohibited")
    if any(part in {".ssh", ".aws", ".azure", ".gcloud", "secrets", "credentials", "private"} for part in parts):
        raise ToolError("Reading or writing credential directories is prohibited")
    if basename in {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "service-account.json",
        "secrets.json",
        "kubeconfig",
        "config.json.enc",
    } or basename.endswith((".pem", ".p12", ".pfx", ".key", ".keystore")):
        raise ToolError("Reading or writing credential material is prohibited")


def _safe_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or any(segment in {".", ".."} for segment in value.split("/"))
        or any(character in value for character in ("?", "#"))
    ):
        raise ToolError("Unsafe repository path")
    return quote(value, safe="/-_.@")


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        handler: Callable[..., Awaitable[dict[str, Any]]],
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Duplicate tool: {name}")
        schema = input_schema or {"type": "object", "additionalProperties": False}
        jsonschema.validators.validator_for(schema).check_schema(schema)
        self._tools[name] = handler
        self._metadata[name] = {
            "name": name,
            "description": description[:1000],
            "input_schema": schema,
        }

    def names(self) -> set[str]:
        return set(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        return [self._metadata[name] for name in sorted(self._metadata)]

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._tools.get(name)
        if handler is None:
            raise ToolError(f"Unknown tool: {name}")
        try:
            jsonschema.validate(arguments, self._metadata[name]["input_schema"])
        except jsonschema.ValidationError as exc:
            raise ToolError("Tool arguments do not match the registered input schema") from exc
        result = await handler(**arguments)
        return redact_sensitive_data(result)


class GitHubTools:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        if not self.token:
            raise ValueError("GITHUB_TOKEN is required to enable GitHub write tools")
        self.api = "https://api.github.com"

    def register(self, registry: ToolRegistry) -> None:
        repository = {"type": "string", "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"}
        branch = {"type": "string", "minLength": 1, "maxLength": 255}
        registry.register(
            "github.list_files",
            self.list_files,
            "List files and directories in the task repository at an allowed branch.",
            {
                "type": "object",
                "properties": {
                    "repository": repository,
                    "path": {"type": "string", "maxLength": 500},
                    "ref": branch,
                },
                "required": ["repository", "ref"],
                "additionalProperties": False,
            },
        )
        registry.register(
            "github.read_file",
            self.read_file,
            "Read a UTF-8 text file from the task repository at an allowed branch.",
            {
                "type": "object",
                "properties": {"repository": repository, "path": {"type": "string", "minLength": 1, "maxLength": 500}, "ref": branch},
                "required": ["repository", "path", "ref"],
                "additionalProperties": False,
            },
        )
        registry.register(
            "github.create_branch",
            self.create_branch,
            "Create the task's isolated feature branch from its configured base branch.",
            {
                "type": "object",
                "properties": {"repository": repository, "base_branch": branch, "work_branch": branch},
                "required": ["repository", "base_branch", "work_branch"],
                "additionalProperties": False,
            },
        )
        registry.register(
            "github.write_file",
            self.write_file,
            "Create or update a UTF-8 text file on the task's exact work branch.",
            {
                "type": "object",
                "properties": {
                    "repository": repository,
                    "path": {"type": "string", "minLength": 1, "maxLength": 500},
                    "content": {"type": "string", "maxLength": 100000},
                    "message": {"type": "string", "minLength": 1, "maxLength": 200},
                    "branch": branch,
                    "expected_sha": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
                },
                "required": ["repository", "path", "content", "message", "branch"],
                "additionalProperties": False,
            },
        )
        registry.register(
            "github.get_ci_status",
            self.get_ci_status,
            "Read GitHub Actions check runs for a specific commit SHA.",
            {
                "type": "object",
                "properties": {"repository": repository, "commit_sha": {"type": "string", "pattern": "^[a-f0-9]{40}$"}},
                "required": ["repository", "commit_sha"],
                "additionalProperties": False,
            },
        )
        registry.register(
            "github.compare_branches",
            self.compare_branches,
            "Compare the task work branch against the base branch and return bounded diff patches for independent review.",
            {
                "type": "object",
                "properties": {
                    "repository": repository,
                    "base": branch,
                    "head": branch,
                },
                "required": ["repository", "base", "head"],
                "additionalProperties": False,
            },
        )
        registry.register(
            "github.create_pull_request",
            self.create_pull_request,
            "Create a draft pull request from the task work branch to its base branch.",
            {
                "type": "object",
                "properties": {
                    "repository": repository,
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "body": {"type": "string", "maxLength": 60000},
                    "head": branch,
                    "base": branch,
                },
                "required": ["repository", "title", "body", "head", "base"],
                "additionalProperties": False,
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.request(
                method,
                f"{self.api}{path}",
                headers=headers,
                **kwargs,
            )
            if response.status_code >= 400:
                raise GitHubAPIError(response.status_code)
            return response.json() if response.content else {}

    async def list_files(
        self,
        repository: str,
        path: str = "",
        ref: str = "main",
    ) -> dict[str, Any]:
        repo = _safe_repo(repository)
        safe_ref = _safe_branch(ref)
        suffix = f"/{_safe_path(path)}" if path else ""
        data = await self._request(
            "GET",
            f"/repos/{repo}/contents{suffix}",
            params={"ref": safe_ref},
        )
        if not isinstance(data, list):
            raise ToolError("GitHub did not return a directory listing")
        return {
            "entries": [
                {"name": item["name"], "path": item["path"], "type": item["type"]}
                for item in data
            ]
        }

    async def read_file(
        self,
        repository: str,
        path: str,
        ref: str = "main",
    ) -> dict[str, Any]:
        repo = _safe_repo(repository)
        safe_path = _safe_path(path)
        _assert_safe_file_path(path)
        safe_ref = _safe_branch(ref)
        data = await self._request(
            "GET",
            f"/repos/{repo}/contents/{safe_path}",
            params={"ref": safe_ref},
        )
        if data.get("type") != "file":
            raise ToolError("Requested path is not a file")
        try:
            content = base64.b64decode(data["content"], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError, KeyError) as exc:
            raise ToolError("File is not a valid UTF-8 text file") from exc
        return {"path": path, "sha": data["sha"], "content": content[:40000]}

    async def create_branch(
        self,
        repository: str,
        base_branch: str,
        work_branch: str,
    ) -> dict[str, Any]:
        repo = _safe_repo(repository)
        base = _safe_branch(base_branch)
        branch = _safe_branch(work_branch)
        if branch == base:
            raise ToolError("Feature branch must differ from base branch")
        try:
            existing = await self._request(
                "GET",
                f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}",
            )
            return {
                "branch": branch,
                "sha": existing["object"]["sha"],
                "already_exists": True,
            }
        except GitHubAPIError as exc:
            if exc.status_code != 404:
                raise
        source = await self._request(
            "GET",
            f"/repos/{repo}/git/ref/heads/{quote(base, safe='/')}",
        )
        try:
            data = await self._request(
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": source["object"]["sha"]},
            )
            return {"branch": branch, "sha": data["object"]["sha"], "already_exists": False}
        except GitHubAPIError as exc:
            if exc.status_code != 422:
                raise
            existing = await self._request(
                "GET",
                f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}",
            )
            return {
                "branch": branch,
                "sha": existing["object"]["sha"],
                "already_exists": True,
            }

    async def write_file(
        self,
        repository: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        expected_sha: str | None = None,
    ) -> dict[str, Any]:
        repo = _safe_repo(repository)
        safe_path = _safe_path(path)
        _assert_safe_file_path(path)
        safe_branch = _safe_branch(branch)
        if len(content) > 100000:
            raise ToolError("File content exceeds the configured limit")
        if expected_sha and not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
            raise ToolError("Invalid expected file SHA")
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
        file_endpoint = f"/repos/{repo}/contents/{safe_path}"
        try:
            existing = await self._request(
                "GET",
                file_endpoint,
                params={"ref": safe_branch},
            )
        except GitHubAPIError as exc:
            if exc.status_code != 404:
                raise
            existing = None

        if existing is not None:
            try:
                existing_content = base64.b64decode(
                    "".join(existing.get("content", "").split()),
                    validate=True,
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                existing_content = None
            if existing_content == content:
                commits = await self._request(
                    "GET",
                    f"/repos/{repo}/commits",
                    params={"path": safe_path, "sha": safe_branch, "per_page": 1},
                )
                if commits:
                    return {
                        "path": path,
                        "commit_sha": commits[0]["sha"],
                        "content_sha": existing["sha"],
                        "already_applied": True,
                    }
            if expected_sha and existing.get("sha") != expected_sha:
                raise ToolError("File changed concurrently; reread and replan before writing")
            if not expected_sha:
                raise ToolError("Existing file must be read and its expected_sha supplied before update")

        payload: dict[str, Any] = {
            "message": message[:200],
            "content": encoded_content,
            "branch": safe_branch,
        }
        if expected_sha:
            payload["sha"] = expected_sha
        data = await self._request("PUT", file_endpoint, json=payload)
        return {
            "path": path,
            "commit_sha": data["commit"]["sha"],
            "content_sha": data["content"]["sha"],
            "already_applied": False,
        }

    async def get_ci_status(
        self,
        repository: str,
        commit_sha: str,
    ) -> dict[str, Any]:
        repo = _safe_repo(repository)
        if not re.fullmatch(r"[a-f0-9]{40}", commit_sha):
            raise ToolError("Invalid commit SHA for CI verification")
        data = await self._request(
            "GET",
            f"/repos/{repo}/commits/{commit_sha}/check-runs",
        )
        checks = [
            {
                "name": item["name"],
                "status": item["status"],
                "conclusion": item.get("conclusion"),
            }
            for item in data.get("check_runs", [])
        ]
        all_passed = bool(checks) and all(
            item["status"] == "completed" and item["conclusion"] == "success"
            for item in checks
        )
        return {
            "commit_sha": commit_sha,
            "total": len(checks),
            "checks": checks,
            "all_passed": all_passed,
        }

    async def compare_branches(
        self,
        repository: str,
        base: str,
        head: str,
    ) -> dict[str, Any]:
        repo = _safe_repo(repository)
        safe_base = _safe_branch(base)
        safe_head = _safe_branch(head)
        if safe_base == safe_head:
            raise ToolError("Branch comparison requires distinct base and head refs")
        data = await self._request(
            "GET",
            f"/repos/{repo}/compare/{quote(safe_base, safe='/')}...{quote(safe_head, safe='/')}",
        )
        raw_files = data.get("files", [])
        if not isinstance(raw_files, list) or len(raw_files) > 20:
            raise ToolError("Diff exceeds the reviewer limit; split the change into smaller tasks")
        files = []
        for item in raw_files:
            patch = item.get("patch") or ""
            if len(patch) > 3000:
                raise ToolError("A diff patch exceeds the reviewer context limit; split the change")
            files.append(
                {
                    "filename": item.get("filename", "")[:500],
                    "status": item.get("status", ""),
                    "additions": item.get("additions", 0),
                    "deletions": item.get("deletions", 0),
                    "patch": patch,
                }
            )
        return {
            "status": data.get("status"),
            "ahead_by": data.get("ahead_by"),
            "behind_by": data.get("behind_by"),
            "total_files": len(data.get("files", [])),
            "files": files,
        }

    async def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        repo = _safe_repo(repository)
        safe_head = _safe_branch(head)
        safe_base = _safe_branch(base)
        if safe_head == safe_base:
            raise ToolError("Pull request head and base must differ")
        owner = repo.split("/", 1)[0]
        existing = await self._request(
            "GET",
            f"/repos/{repo}/pulls",
            params={
                "state": "open",
                "head": f"{owner}:{safe_head}",
                "base": safe_base,
                "per_page": 1,
            },
        )
        if existing:
            pull = existing[0]
            return {
                "number": pull["number"],
                "url": pull["html_url"],
                "state": pull["state"],
                "draft": pull["draft"],
                "already_exists": True,
            }
        data = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={
                "title": title[:200],
                "body": body[:60000],
                "head": safe_head,
                "base": safe_base,
                "draft": True,
            },
        )
        return {
            "number": data["number"],
            "url": data["html_url"],
            "state": data["state"],
            "draft": data["draft"],
            "already_exists": False,
        }
