"""Least-privilege GitHub adapter. It never merges, deploys, or runs arbitrary shell."""

import base64
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .contracts import CodingTask, ToolCall, ToolResult
from .policy import AutonomyPolicy


class GitHubToolGateway:
    def __init__(
        self, *, token: str, api_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0, max_file_bytes: int = 1_000_000,
    ) -> None:
        parsed = urlparse(api_url)
        if (
            parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("GitHub API URL must use HTTPS without credentials or query")
        if not token:
            raise ValueError("GitHub token is required")
        if timeout_seconds <= 0 or max_file_bytes < 1024:
            raise ValueError("invalid GitHub gateway limits")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self.max_file_bytes = max_file_bytes

    def _root(self, task: CodingTask) -> str:
        owner, name = task.repository.full_name.split("/", 1)
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None, allow_not_found: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response = await client.request(
                method, f"{self.api_url}{path}", headers=headers, json=json_body, params=params,
            )
        if allow_not_found and response.status_code == 404:
            return None
        if response.is_error:
            raise RuntimeError(f"GitHub API operation failed with HTTP {response.status_code}")
        if not response.content:
            return {}
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("GitHub API returned invalid JSON") from exc
        if not isinstance(result, (dict, list)):
            raise RuntimeError("GitHub API returned an unexpected response")
        return result

    def _check_regular_file_size(self, result: dict[str, Any]) -> int:
        try:
            size = int(result.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("GitHub returned an invalid file size") from exc
        if size < 0 or size > self.max_file_bytes:
            raise RuntimeError("repository file exceeds the configured size limit")
        if size and result.get("encoding") != "base64":
            raise RuntimeError("repository file is not available as bounded base64 content")
        return size

    async def _ensure_branch(self, task: CodingTask) -> dict[str, Any]:
        root = self._root(task)
        branch = quote(task.repository.work_branch, safe="/")
        existing = await self._request("GET", f"{root}/git/ref/heads/{branch}", allow_not_found=True)
        if existing is not None:
            return existing
        base = await self._request(
            "GET", f"{root}/git/ref/heads/{quote(task.repository.base_branch, safe='/')}",
        )
        if not isinstance(base, dict):
            raise RuntimeError("repository base branch was not found")
        created = await self._request(
            "POST", f"{root}/git/refs",
            json_body={
                "ref": f"refs/heads/{task.repository.work_branch}",
                "sha": base["object"]["sha"],
            },
        )
        if not isinstance(created, dict):
            raise RuntimeError("work branch could not be created")
        return created

    async def _read_file(self, task: CodingTask, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path", ""))
        if not AutonomyPolicy.safe_repository_path(path):
            raise PermissionError("repository path is protected or unsafe")
        ref = str(arguments.get("ref", task.repository.base_branch))
        if ref not in {task.repository.base_branch, task.repository.work_branch}:
            raise PermissionError("read ref is outside the task repository scope")
        result = await self._request(
            "GET", f"{self._root(task)}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
        )
        if not isinstance(result, dict) or result.get("type") != "file":
            raise RuntimeError("repository file was not found or is not a regular file")
        self._check_regular_file_size(result)
        encoded = result.get("content", "").replace("\n", "")
        content = base64.b64decode(encoded)
        if len(content) > self.max_file_bytes:
            raise RuntimeError("repository file exceeds the configured read limit")
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("binary files are not supported by the agent read tool") from exc
        return ToolResult(
            name="read_file", success=True,
            data={"path": path, "ref": ref, "content": decoded},
            evidence={"source": "github_contents_api", "sha": result.get("sha")},
        )

    async def _write_file(self, task: CodingTask, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path", ""))
        content = arguments.get("content")
        if not AutonomyPolicy.safe_repository_path(path):
            raise PermissionError("repository path is protected or unsafe")
        if not isinstance(content, str) or len(content.encode("utf-8")) > self.max_file_bytes:
            raise ValueError("file content must be text within the configured size limit")
        await self._ensure_branch(task)
        endpoint = f"{self._root(task)}/contents/{quote(path, safe='/')}"
        current = await self._request(
            "GET", endpoint, params={"ref": task.repository.work_branch}, allow_not_found=True,
        )
        if isinstance(current, dict):
            if current.get("type") != "file":
                raise PermissionError("agent writes cannot replace symlinks or non-regular files")
            self._check_regular_file_size(current)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        if isinstance(current, dict):
            old_content = base64.b64decode(current.get("content", "").replace("\n", ""))
            if old_content == content.encode("utf-8"):
                return ToolResult(
                    name="write_file", success=True, data={"path": path, "unchanged": True},
                    evidence={"source": "github_contents_api", "sha": current.get("sha")},
                )
        body: dict[str, Any] = {
            "message": str(arguments.get("message", "RUSA-AI: update repository file"))[:200],
            "content": encoded,
            "branch": task.repository.work_branch,
        }
        if isinstance(current, dict):
            body["sha"] = current["sha"]
        result = await self._request("PUT", endpoint, json_body=body)
        if not isinstance(result, dict) or not isinstance(result.get("commit"), dict):
            raise RuntimeError("GitHub did not confirm the file commit")
        return ToolResult(
            name="write_file", success=True,
            data={"path": path, "commit_sha": result["commit"].get("sha")},
            evidence={"source": "github_contents_api", "branch": task.repository.work_branch},
        )

    async def _create_pull_request(self, task: CodingTask, arguments: dict[str, Any]) -> ToolResult:
        await self._ensure_branch(task)
        owner = task.repository.full_name.split("/", 1)[0]
        existing = await self._request(
            "GET", f"{self._root(task)}/pulls",
            params={
                "state": "open", "head": f"{owner}:{task.repository.work_branch}",
                "base": task.repository.base_branch, "per_page": "100",
            },
        )
        if isinstance(existing, list) and existing:
            current = existing[0]
            return ToolResult(
                name="create_pull_request", success=True,
                data={
                    "number": current.get("number"), "url": current.get("html_url"),
                    "draft": current.get("draft"),
                },
                evidence={"source": "github_pull_requests_api", "idempotent_reuse": True},
            )
        result = await self._request(
            "POST", f"{self._root(task)}/pulls",
            json_body={
                "title": str(arguments.get("title", task.objective))[:240],
                "body": str(arguments.get("body", ""))[:6000],
                "head": task.repository.work_branch,
                "base": task.repository.base_branch,
                "draft": True,
            },
        )
        if not isinstance(result, dict) or not result.get("number"):
            raise RuntimeError("GitHub did not confirm pull request creation")
        return ToolResult(
            name="create_pull_request", success=True,
            data={"number": result["number"], "url": result.get("html_url"), "draft": True},
            evidence={"source": "github_pull_requests_api"},
        )

    async def execute(self, task: CodingTask, call: ToolCall) -> ToolResult:
        arguments = dict(call.arguments)
        root = self._root(task)
        if call.name == "create_branch":
            result = await self._ensure_branch(task)
            return ToolResult(
                name=call.name, success=True, data={"branch": task.repository.work_branch},
                evidence={"source": "github_git_refs", "sha": result.get("object", {}).get("sha")},
            )
        if call.name == "read_file":
            return await self._read_file(task, arguments)
        if call.name == "list_files":
            path = str(arguments.get("path", ""))
            if path and not AutonomyPolicy.safe_repository_path(path):
                raise PermissionError("repository path is protected or unsafe")
            ref = str(arguments.get("ref", task.repository.base_branch))
            if ref not in {task.repository.base_branch, task.repository.work_branch}:
                raise PermissionError("read ref is outside the task repository scope")
            endpoint = f"{root}/contents" + (f"/{quote(path, safe='/')}" if path else "")
            result = await self._request("GET", endpoint, params={"ref": ref})
            items = result if isinstance(result, list) else [result]
            return ToolResult(
                name=call.name, success=True,
                data={
                    "items": [
                        {"path": item.get("path"), "type": item.get("type"), "sha": item.get("sha")}
                        for item in items[:200] if isinstance(item, dict)
                    ]
                },
                evidence={"source": "github_contents_api", "ref": ref},
            )
        if call.name == "write_file":
            return await self._write_file(task, arguments)
        if call.name == "create_pull_request":
            return await self._create_pull_request(task, arguments)
        if call.name == "get_ci_status":
            branch = await self._request(
                "GET", f"{root}/branches/{quote(task.repository.work_branch, safe='/')}",
            )
            if not isinstance(branch, dict):
                raise RuntimeError("work branch was not found")
            sha = branch["commit"]["sha"]
            checks = await self._request(
                "GET", f"{root}/commits/{sha}/check-runs", params={"per_page": "100"},
            )
            if not isinstance(checks, dict):
                raise RuntimeError("GitHub check-runs response was invalid")
            items = [
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "conclusion": item.get("conclusion"),
                    "details_url": item.get("details_url"),
                }
                for item in checks.get("check_runs", [])
            ]
            checks_complete = (
                type(checks.get("total_count")) is int
                and checks["total_count"] == len(items)
            )
            complete = checks_complete and bool(items) and all(
                item["status"] == "completed" for item in items
            )
            passed = complete and all(item["conclusion"] == "success" for item in items)
            return ToolResult(
                name=call.name, success=True,
                data={"head_sha": sha, "checks": items, "checks_complete": checks_complete, "all_checks_success": passed},
                evidence={"source": "github_check_runs_api", "head_sha": sha},
            )
        raise PermissionError(f"tool '{call.name}' is not implemented")
