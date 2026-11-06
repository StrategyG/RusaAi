"""Strict tool registry with bounded GitHub operations and CI evidence lookup."""
from __future__ import annotations

import base64
import os
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx


class ToolError(RuntimeError):
    pass


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

    def register(self, name: str, handler: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        if name in self._tools:
            raise ValueError(f"Duplicate tool: {name}")
        self._tools[name] = handler

    def names(self) -> set[str]:
        return set(self._tools)

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._tools.get(name)
        if handler is None:
            raise ToolError(f"Unknown tool: {name}")
        return await handler(**arguments)


class GitHubTools:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        if not self.token:
            raise ValueError("GITHUB_TOKEN is required to enable GitHub write tools")
        self.api = "https://api.github.com"

    def register(self, registry: ToolRegistry) -> None:
        registry.register("github.list_files", self.list_files)
        registry.register("github.read_file", self.read_file)
        registry.register("github.create_branch", self.create_branch)
        registry.register("github.write_file", self.write_file)
        registry.register("github.get_ci_status", self.get_ci_status)
        registry.register("github.create_pull_request", self.create_pull_request)

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
                raise ToolError(f"GitHub API returned HTTP {response.status_code}")
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
        source = await self._request(
            "GET",
            f"/repos/{repo}/git/ref/heads/{quote(base, safe='/')}",
        )
        data = await self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": source["object"]["sha"]},
        )
        return {"branch": branch, "sha": data["object"]["sha"]}

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
        safe_branch = _safe_branch(branch)
        if len(content) > 100000:
            raise ToolError("File content exceeds the configured limit")
        payload: dict[str, Any] = {
            "message": message[:200],
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": safe_branch,
        }
        if expected_sha:
            if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
                raise ToolError("Invalid expected file SHA")
            payload["sha"] = expected_sha
        data = await self._request(
            "PUT",
            f"/repos/{repo}/contents/{safe_path}",
            json=payload,
        )
        return {
            "path": path,
            "commit_sha": data["commit"]["sha"],
            "content_sha": data["content"]["sha"],
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
        }
