"""Least-privilege GitHub adapter with repository and tenant scoping."""

from __future__ import annotations

import base64
import json
import math
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .contracts import CodingTask, ToolCall, ToolResult
from .policy import AutonomyPolicy


_SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"), "[REDACTED TOKEN]"),
    (re.compile(r"""(?i)(api[_-]?key|client[_-]?secret|secret|password|access[_-]?token|token)(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)"""), r"\1\2[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED JWT]"),
)


def redact_sensitive_text(value: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


class GitHubToolGateway:
    def __init__(
        self,
        *,
        token: str,
        allowed_repositories: set[str],
        tenant_repositories: dict[str, set[str]] | None = None,
        api_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 4_000_000,
        max_file_bytes: int = 1_000_000,
        max_check_pages: int = 10,
        max_diff_pages: int = 3,
        max_diff_files: int = 20,
        max_diff_patch_chars: int = 5500,
        max_diff_chars: int = 100_000,
    ) -> None:
        parsed = urlparse(api_url)
        if (
            parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("GitHub API URL must use HTTPS without credentials or query")
        if not token:
            raise ValueError("GitHub token is required")
        if not isinstance(allowed_repositories, (set, list, tuple)) or not all(
            isinstance(item, str) for item in allowed_repositories
        ):
            raise ValueError("global repository allowlist must contain strings")
        repositories = {item.strip().lower() for item in allowed_repositories if item.strip()}
        if not repositories or any(
            not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", item)
            for item in repositories
        ):
            raise ValueError("an explicit owner/name repository allowlist is required")
        normalized_tenant_map = None
        if tenant_repositories is not None:
            if not tenant_repositories:
                raise ValueError("tenant repository mapping cannot be empty")
            normalized_tenant_map = {}
            for tenant_id, values in tenant_repositories.items():
                if (
                    not isinstance(tenant_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tenant_id)
                    or not isinstance(values, (set, list, tuple))
                    or not values
                    or not all(isinstance(item, str) for item in values)
                ):
                    raise ValueError("tenant repository mapping contains an invalid entry")
                normalized = {item.strip().lower() for item in values if item.strip()}
                if not normalized or not normalized.issubset(repositories):
                    raise ValueError("tenant repositories must be a subset of the global allowlist")
                normalized_tenant_map[tenant_id] = normalized
        if (
            timeout_seconds <= 0 or max_response_bytes < 1024 or max_file_bytes < 1024
            or not 1 <= max_check_pages <= 10 or not 1 <= max_diff_pages <= 3
            or not 1 <= max_diff_files <= 100 or not 1024 <= max_diff_patch_chars <= 12000
            or max_diff_chars < max_diff_patch_chars
        ):
            raise ValueError("invalid GitHub gateway limits")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.allowed_repositories = repositories
        self.tenant_repositories = normalized_tenant_map
        self.timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self.max_response_bytes = max_response_bytes
        self.max_file_bytes = max_file_bytes
        self.max_check_pages = max_check_pages
        self.max_diff_pages = max_diff_pages
        self.max_diff_files = max_diff_files
        self.max_diff_patch_chars = max_diff_patch_chars
        self.max_diff_chars = max_diff_chars

    def _validate_task(self, task: CodingTask) -> None:
        repository = task.repository.full_name.lower()
        if repository not in self.allowed_repositories:
            raise PermissionError("repository is not in the configured GitHub allowlist")
        if self.tenant_repositories is not None:
            tenant_allowed = self.tenant_repositories.get(task.tenant_id, set())
            if repository not in tenant_allowed:
                raise PermissionError("repository is not assigned to this tenant")
        if task.repository.work_branch != task.expected_work_branch():
            raise PermissionError("task work branch is outside its tenant-scoped namespace")

    def _root(self, task: CodingTask) -> str:
        owner, name = task.repository.full_name.split("/", 1)
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "RUSA-AI-Agent/1.0",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                json=json_body,
                params=params,
            ) as response:
                if allow_not_found and response.status_code == 404:
                    return None
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(
                        f"GitHub API operation failed with HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.max_response_bytes:
                        raise RuntimeError("GitHub API response exceeds the configured size limit")
                    body.extend(chunk)
        if not body:
            return {}
        try:
            result = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("GitHub API returned invalid JSON") from exc
        if not isinstance(result, (dict, list)):
            raise RuntimeError("GitHub API returned an unexpected response")
        return result

    async def _ensure_branch(self, task: CodingTask) -> dict[str, Any]:
        root = self._root(task)
        branch = quote(task.repository.work_branch, safe="/")
        existing = await self._request(
            "GET", f"{root}/git/ref/heads/{branch}", allow_not_found=True,
        )
        if existing is not None:
            return existing
        base = await self._request(
            "GET",
            f"{root}/git/ref/heads/{quote(task.repository.base_branch, safe='/')}",
        )
        if not isinstance(base, dict):
            raise RuntimeError("repository base branch was not found")
        try:
            created = await self._request(
                "POST",
                f"{root}/git/refs",
                json_body={
                    "ref": f"refs/heads/{task.repository.work_branch}",
                    "sha": base["object"]["sha"],
                },
            )
        except RuntimeError:
            existing = await self._request(
                "GET", f"{root}/git/ref/heads/{branch}", allow_not_found=True,
            )
            if existing is not None:
                return existing
            raise
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
            "GET",
            f"{self._root(task)}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
        )
        if not isinstance(result, dict) or result.get("type") != "file":
            raise RuntimeError("repository file was not found or is not a regular file")
        if int(result.get("size", 0)) > self.max_file_bytes:
            raise RuntimeError("repository file exceeds the configured read limit")
        encoded = result.get("content", "").replace("\n", "")
        try:
            content = base64.b64decode(encoded, validate=True)
            decoded = content.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("binary or invalidly encoded files are not supported") from exc
        if len(content) > self.max_file_bytes:
            raise RuntimeError("repository file exceeds the configured read limit")
        return ToolResult(
            name="read_file",
            success=True,
            data={"path": path, "ref": ref, "content": redact_sensitive_text(decoded)},
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
            "GET",
            endpoint,
            params={"ref": task.repository.work_branch},
            allow_not_found=True,
        )
        if isinstance(current, dict):
            if current.get("type") != "file":
                raise PermissionError("agent writes cannot replace symlinks or non-regular files")
            if int(current.get("size", 0)) > self.max_file_bytes:
                raise RuntimeError("existing repository file exceeds the configured write limit")
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        if isinstance(current, dict):
            old_encoded = current.get("content", "").replace("\n", "")
            try:
                old_content = base64.b64decode(old_encoded, validate=True)
            except ValueError as exc:
                raise RuntimeError("existing file content could not be decoded safely") from exc
            if old_content == content.encode("utf-8"):
                return ToolResult(
                    name="write_file",
                    success=True,
                    data={"path": path, "unchanged": True},
                    evidence={"source": "github_contents_api", "sha": current.get("sha")},
                )
        body: dict[str, Any] = {
            "message": str(arguments.get("message", "RUSA-AI: update repository file"))[:200],
            "content": encoded,
            "branch": task.repository.work_branch,
        }
        if isinstance(current, dict):
            body["sha"] = current["sha"]
        try:
            result = await self._request("PUT", endpoint, json_body=body)
        except RuntimeError:
            # The request may have committed even if the client lost the response.
            recovered = await self._request(
                "GET",
                endpoint,
                params={"ref": task.repository.work_branch},
                allow_not_found=True,
            )
            if not isinstance(recovered, dict) or recovered.get("type") != "file":
                raise
            recovered_content = recovered.get("content")
            if not isinstance(recovered_content, str):
                raise
            try:
                recovered_bytes = base64.b64decode(
                    recovered_content.replace("\n", ""),
                    validate=True,
                )
            except ValueError:
                raise
            if recovered_bytes != content.encode("utf-8"):
                raise
            return ToolResult(
                name="write_file",
                success=True,
                data={"path": path, "commit_sha": None, "recovered_after_ambiguous_commit": True},
                evidence={"source": "github_contents_api", "branch": task.repository.work_branch},
            )
        if not isinstance(result, dict) or not isinstance(result.get("commit"), dict):
            raise RuntimeError("GitHub did not confirm the file commit")
        return ToolResult(
            name="write_file",
            success=True,
            data={"path": path, "commit_sha": result["commit"].get("sha")},
            evidence={"source": "github_contents_api", "branch": task.repository.work_branch},
        )

    async def _create_pull_request(
        self, task: CodingTask, arguments: dict[str, Any],
    ) -> ToolResult:
        await self._ensure_branch(task)
        root = self._root(task)
        owner = task.repository.full_name.split("/", 1)[0]
        query = {
            "state": "open",
            "head": f"{owner}:{task.repository.work_branch}",
            "base": task.repository.base_branch,
            "per_page": "100",
        }
        existing = await self._request("GET", f"{root}/pulls", params=query)
        if isinstance(existing, list) and existing:
            current = existing[0]
            return ToolResult(
                name="create_pull_request",
                success=True,
                data={
                    "number": current.get("number"),
                    "url": current.get("html_url"),
                    "draft": current.get("draft"),
                    "head_sha": current.get("head", {}).get("sha"),
                },
                evidence={"source": "github_pull_requests_api", "idempotent_reuse": True},
            )
        try:
            result = await self._request(
                "POST",
                f"{root}/pulls",
                json_body={
                    "title": str(arguments.get("title", task.objective))[:240],
                    "body": str(arguments.get("body", ""))[:6000],
                    "head": task.repository.work_branch,
                    "base": task.repository.base_branch,
                    "draft": True,
                },
            )
        except RuntimeError:
            existing = await self._request("GET", f"{root}/pulls", params=query)
            if isinstance(existing, list) and existing:
                result = existing[0]
            else:
                raise
        if not isinstance(result, dict) or not result.get("number"):
            raise RuntimeError("GitHub did not confirm pull request creation")
        return ToolResult(
            name="create_pull_request",
            success=True,
            data={
                "number": result["number"],
                "url": result.get("html_url"),
                "draft": result.get("draft", True),
                "head_sha": result.get("head", {}).get("sha"),
            },
            evidence={"source": "github_pull_requests_api"},
        )

    async def _get_ci_status(self, task: CodingTask) -> ToolResult:
        root = self._root(task)
        branch = await self._request(
            "GET",
            f"{root}/branches/{quote(task.repository.work_branch, safe='/')}",
        )
        if not isinstance(branch, dict):
            raise RuntimeError("work branch was not found")
        sha = str(branch.get("commit", {}).get("sha", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise RuntimeError("GitHub returned an invalid work-branch commit SHA")

        first = await self._request(
            "GET",
            f"{root}/commits/{sha}/check-runs",
            params={"per_page": "100", "page": "1"},
        )
        if not isinstance(first, dict):
            raise RuntimeError("GitHub check-runs response was invalid")
        total_count = first.get("total_count")
        if type(total_count) is not int or total_count < 0:
            return ToolResult(
                name="get_ci_status",
                success=True,
                data={"head_sha": sha, "checks": [], "checks_complete": False,
                      "total_count": None, "all_checks_success": False},
                evidence={"source": "github_check_runs_api", "head_sha": sha},
            )

        all_runs: list[dict[str, Any]] = []
        page_count = max(1, math.ceil(total_count / 100))
        page_limit = min(page_count, self.max_check_pages)
        for page in range(1, page_limit + 1):
            payload = first if page == 1 else await self._request(
                "GET",
                f"{root}/commits/{sha}/check-runs",
                params={"per_page": "100", "page": str(page)},
            )
            if not isinstance(payload, dict):
                break
            if payload.get("total_count") != total_count:
                break
            runs = payload.get("check_runs")
            if not isinstance(runs, list):
                break
            all_runs.extend(item for item in runs if isinstance(item, dict))

        run_ids = [item.get("id") for item in all_runs if item.get("id") is not None]
        checks_complete = (
            total_count <= self.max_check_pages * 100
            and len(all_runs) == total_count
            and len(run_ids) == len(all_runs)
            and len(run_ids) == len(set(run_ids))
        )
        checks = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "details_url": item.get("details_url"),
            }
            for item in all_runs
        ]
        all_success = (
            checks_complete and bool(checks)
            and all(item.get("status") == "completed" and item.get("conclusion") == "success"
                    for item in checks)
        )
        return ToolResult(
            name="get_ci_status",
            success=True,
            data={
                "head_sha": sha,
                "checks": checks,
                "checks_complete": checks_complete,
                "total_count": total_count,
                "all_checks_success": all_success,
            },
            evidence={"source": "github_check_runs_api", "head_sha": sha},
        )

    async def _get_branch_diff(self, task: CodingTask) -> ToolResult:
        root = self._root(task)
        base = await self._request(
            "GET",
            f"{root}/branches/{quote(task.repository.base_branch, safe='/')}",
        )
        head = await self._request(
            "GET",
            f"{root}/branches/{quote(task.repository.work_branch, safe='/')}",
        )
        if not isinstance(base, dict) or not isinstance(head, dict):
            raise RuntimeError("base or work branch was not found")
        base_sha = str(base.get("commit", {}).get("sha", ""))
        head_sha = str(head.get("commit", {}).get("sha", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha) or not re.fullmatch(
            r"[0-9a-fA-F]{40}", head_sha
        ):
            raise RuntimeError("GitHub returned an invalid branch commit SHA")
        if base_sha == head_sha:
            return ToolResult(
                name="get_branch_diff",
                success=True,
                data={
                    "base_sha": base_sha, "head_sha": head_sha, "changed_files": 0,
                    "files": [], "diff_complete": True,
                },
                evidence={"source": "github_compare_api", "head_sha": head_sha},
            )

        compare_ref = quote(f"{base_sha}...{head_sha}", safe=".")
        first = await self._request(
            "GET",
            f"{root}/compare/{compare_ref}",
            params={"per_page": "100", "page": "1"},
        )
        if not isinstance(first, dict):
            raise RuntimeError("GitHub compare response was invalid")
        first_files = first.get("files")
        changed_count = first.get("changed_files")
        if not isinstance(first_files, list) or type(changed_count) is not int:
            raise RuntimeError("GitHub compare response omitted file metadata")

        page_count = max(1, math.ceil(changed_count / 100))
        page_limit = min(page_count, self.max_diff_pages)
        collected: list[dict[str, Any]] = []
        diff_chars = 0
        diff_complete = (
            changed_count <= self.max_diff_pages * 100
            and changed_count <= self.max_diff_files
        )
        for page in range(1, page_limit + 1):
            payload = first if page == 1 else await self._request(
                "GET",
                f"{root}/compare/{compare_ref}",
                params={"per_page": "100", "page": str(page)},
            )
            if not isinstance(payload, dict) or payload.get("changed_files") != changed_count:
                diff_complete = False
                break
            page_files = payload.get("files")
            if not isinstance(page_files, list):
                diff_complete = False
                break
            for item in page_files:
                if len(collected) >= self.max_diff_files:
                    diff_complete = False
                    break
                if not isinstance(item, dict):
                    diff_complete = False
                    continue
                filename = str(item.get("filename", ""))
                patch = item.get("patch")
                if not filename or not AutonomyPolicy.safe_repository_path(filename):
                    patch = ""
                    diff_complete = False
                elif not isinstance(patch, str):
                    patch = ""
                    diff_complete = False
                if (
                    len(patch) > self.max_diff_patch_chars
                    or diff_chars + len(patch) > self.max_diff_chars
                ):
                    patch = patch[:max(
                        0,
                        min(self.max_diff_patch_chars, self.max_diff_chars - diff_chars),
                    )]
                    diff_complete = False
                diff_chars += len(patch)
                collected.append({
                    "path": filename,
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "patch": redact_sensitive_text(patch),
                })

        if len(collected) != changed_count:
            diff_complete = False
        return ToolResult(
            name="get_branch_diff",
            success=True,
            data={
                "base_sha": base_sha,
                "head_sha": head_sha,
                "changed_files": changed_count,
                "files": collected,
                "diff_complete": diff_complete,
            },
            evidence={"source": "github_compare_api", "head_sha": head_sha},
        )

    async def execute(self, task: CodingTask, call: ToolCall) -> ToolResult:
        self._validate_task(task)
        arguments = dict(call.arguments)
        root = self._root(task)
        if call.name == "create_branch":
            result = await self._ensure_branch(task)
            return ToolResult(
                name=call.name,
                success=True,
                data={"branch": task.repository.work_branch},
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
            endpoint = f"{root}/contents" + (
                f"/{quote(path, safe='/')}" if path else ""
            )
            result = await self._request("GET", endpoint, params={"ref": ref})
            items = result if isinstance(result, list) else [result]
            return ToolResult(
                name=call.name,
                success=True,
                data={
                    "items": [
                        {
                            "path": item.get("path"),
                            "type": item.get("type"),
                            "sha": item.get("sha"),
                        }
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
            return await self._get_ci_status(task)
        if call.name == "get_branch_diff":
            return await self._get_branch_diff(task)
        raise PermissionError(f"tool '{call.name}' is not implemented")
