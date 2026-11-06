"""Least-privilege GitHub adapter. It never merges, deploys, or runs arbitrary shell."""

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
    (re.compile(r"""(?i)(api[_-]?key|client[_-]?secret|password|access[_-]?token)(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)"""), r"\1\2[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED JWT]"),
)


def redact_sensitive_text(value: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


class GitHubToolGateway:
    def __init__(
        self, *, token: str, api_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0, max_file_bytes: int = 1_000_000,
        max_response_bytes: int = 2_000_000,
        max_check_pages: int = 10,
        max_diff_pages: int = 3,
        max_diff_files: int = 20,
        max_diff_patch_chars: int = 5500,
        max_diff_chars: int = 100_000,
        allowed_repositories_by_tenant: dict[str, set[str]] | None = None,
        policy: AutonomyPolicy | None = None,
    ) -> None:
        parsed = urlparse(api_url)
        if (
            parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("GitHub API URL must use HTTPS without credentials or query")
        if not token:
            raise ValueError("GitHub token is required")
        if (
            timeout_seconds <= 0 or max_file_bytes < 1024 or max_response_bytes < 1024
            or not 1 <= max_check_pages <= 10 or not 1 <= max_diff_pages <= 3
            or not 1 <= max_diff_files <= 100 or not 1024 <= max_diff_patch_chars <= 12000
            or max_diff_chars < max_diff_patch_chars
        ):
            raise ValueError("invalid GitHub gateway limits")
        if not allowed_repositories_by_tenant:
            raise ValueError("an explicit per-tenant repository allowlist is required")
        for tenant_id, repositories in allowed_repositories_by_tenant.items():
            if (
                not tenant_id
                or not repositories
                or any(
                    not isinstance(repository, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                    for repository in repositories
                )
            ):
                raise ValueError("per-tenant repository allowlist is invalid")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.policy = policy or AutonomyPolicy()
        self.timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self.max_file_bytes = max_file_bytes
        self.max_response_bytes = max_response_bytes
        self.max_check_pages = max_check_pages
        self.max_diff_pages = max_diff_pages
        self.max_diff_files = max_diff_files
        self.max_diff_patch_chars = max_diff_patch_chars
        self.max_diff_chars = max_diff_chars
        self.allowed_repositories_by_tenant = {
            tenant_id: frozenset(repositories)
            for tenant_id, repositories in allowed_repositories_by_tenant.items()
        }

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
        response_body = bytearray()
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            async with client.stream(
                method, f"{self.api_url}{path}", headers=headers, json=json_body, params=params,
            ) as response:
                if allow_not_found and response.status_code == 404:
                    return None
                if response.is_error:
                    raise RuntimeError(
                        f"GitHub API operation failed with HTTP {response.status_code}"
                    )
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > self.max_response_bytes:
                            raise RuntimeError("GitHub API response exceeds the configured size limit")
                    except ValueError as exc:
                        raise RuntimeError("GitHub API returned an invalid content length") from exc
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(response_body) + len(chunk) > self.max_response_bytes:
                        raise RuntimeError("GitHub API response exceeds the configured size limit")
                    response_body.extend(chunk)
        if not response_body:
            return {}
        try:
            result = json.loads(response_body)
        except (ValueError, UnicodeDecodeError) as exc:
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
        if self.policy.requires_human_review_path(path):
            raise PermissionError("agent control-plane files require human approval before modification")
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

    async def _get_ci_status(self, task: CodingTask) -> ToolResult:
        root = self._root(task)
        branch = await self._request(
            "GET", f"{root}/branches/{quote(task.repository.work_branch, safe='')}",
        )
        if not isinstance(branch, dict):
            raise RuntimeError("work branch was not found")
        sha = str(branch.get("commit", {}).get("sha", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise RuntimeError("GitHub returned an invalid work-branch SHA")

        total_count = None
        pagination_valid = True
        raw_items = []
        for page in range(1, self.max_check_pages + 1):
            payload = await self._request(
                "GET",
                f"{root}/commits/{sha}/check-runs",
                params={"per_page": "100", "page": str(page)},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
                raise RuntimeError("GitHub check-runs response was invalid")
            page_total = payload.get("total_count")
            if type(page_total) is not int or page_total < 0:
                total_count = None
                pagination_valid = False
                break
            if total_count is None:
                total_count = page_total
            elif page_total != total_count:
                break
            seen = {
                item.get("id") if item.get("id") is not None else (
                    item.get("name"), item.get("status"), item.get("conclusion"),
                    item.get("details_url"),
                )
                for item in raw_items if isinstance(item, dict)
            }
            for item in payload["check_runs"]:
                if not isinstance(item, dict):
                    continue
                identity = item.get("id") if item.get("id") is not None else (
                    item.get("name"), item.get("status"), item.get("conclusion"),
                    item.get("details_url"),
                )
                if identity not in seen:
                    raw_items.append(item)
                    seen.add(identity)
            if len(raw_items) >= total_count:
                break

        raw_total_count = total_count
        run_ids = [item.get("id") for item in raw_items if isinstance(item, dict)]
        checks_complete = (
            pagination_valid
            and raw_total_count is not None
            and len(raw_items) == raw_total_count
            and all(type(run_id) is int for run_id in run_ids)
            and len(run_ids) == len(set(run_ids))
        )
        latest = await self._request(
            "GET", f"{root}/branches/{quote(task.repository.work_branch, safe='')}",
        )
        latest_sha = str(latest.get("commit", {}).get("sha", "")) if isinstance(latest, dict) else ""
        latest_by_identity = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            app = item.get("app") if isinstance(item.get("app"), dict) else {}
            identity = (app.get("id"), item.get("name"))
            timestamp = str(item.get("started_at") or item.get("created_at") or "")
            current = latest_by_identity.get(identity)
            if current is None or timestamp >= current[0]:
                latest_by_identity[identity] = (timestamp, item)
        latest_items = [entry[1] for entry in latest_by_identity.values()]
        items = [
            {
                "id": item.get("id"),
                "app_id": (item.get("app") or {}).get("id")
                    if isinstance(item.get("app"), dict) else None,
                "name": item.get("name"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "details_url": item.get("details_url"),
                "started_at": item.get("started_at"),
            }
            for item in latest_items
        ]
        items.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("app_id") or "")))
        complete = checks_complete and bool(items) and all(
            item["status"] == "completed" for item in items
        )
        passed = complete and all(item["conclusion"] == "success" for item in items)
        return ToolResult(
            name="get_ci_status",
            success=True,
            data={
                "head_sha": latest_sha if latest_sha != sha else sha,
                "checks": items,
                "checks_complete": checks_complete and latest_sha == sha,
                "total_count": len(items),
                "raw_total_count": raw_total_count,
                "all_checks_success": passed and latest_sha == sha,
            },
            evidence={"source": "github_check_runs_api", "head_sha": sha},
        )

    async def _get_branch_diff(self, task: CodingTask) -> ToolResult:
        root = self._root(task)
        base = await self._request(
            "GET", f"{root}/branches/{quote(task.repository.base_branch, safe='')}",
        )
        head = await self._request(
            "GET", f"{root}/branches/{quote(task.repository.work_branch, safe='')}",
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
                    "sensitive_content_detected": False,
                },
                evidence={"source": "github_compare_api", "head_sha": head_sha},
            )

        compare_ref = quote(f"{base_sha}...{head_sha}", safe=".")
        first = await self._request(
            "GET", f"{root}/compare/{compare_ref}",
            params={"per_page": "100", "page": "1"},
        )
        if not isinstance(first, dict):
            raise RuntimeError("GitHub compare response was invalid")
        changed_count = first.get("changed_files")
        if type(changed_count) is not int or not isinstance(first.get("files"), list):
            raise RuntimeError("GitHub compare response omitted file metadata")
        diff_complete = (
            first.get("status") in {"ahead", "identical"}
            and first.get("behind_by", 0) == 0
            and changed_count <= self.max_diff_pages * 100
            and changed_count <= self.max_diff_files
        )
        page_count = max(1, math.ceil(changed_count / 100))
        page_limit = min(page_count, self.max_diff_pages)
        collected = []
        diff_chars = 0
        sensitive_content_detected = False
        for page in range(1, page_limit + 1):
            payload = first if page == 1 else await self._request(
                "GET", f"{root}/compare/{compare_ref}",
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
                if len(patch) > self.max_diff_patch_chars or diff_chars + len(patch) > self.max_diff_chars:
                    patch = patch[:max(
                        0, min(self.max_diff_patch_chars, self.max_diff_chars - diff_chars),
                    )]
                    diff_complete = False
                diff_chars += len(patch)
                redacted_patch = redact_sensitive_text(patch)
                if redacted_patch != patch:
                    sensitive_content_detected = True
                collected.append({
                    "path": filename,
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "patch": redacted_patch,
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
                "sensitive_content_detected": sensitive_content_detected,
            },
            evidence={"source": "github_compare_api", "head_sha": head_sha},
        )

    async def execute(self, task: CodingTask, call: ToolCall) -> ToolResult:
        allowed = self.allowed_repositories_by_tenant.get(task.tenant_id, frozenset())
        if task.repository.full_name not in allowed:
            raise PermissionError("repository is not allowlisted for this tenant")
        if task.repository.work_branch != task.expected_work_branch():
            raise PermissionError("work branch is not scoped to this tenant and task")
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
            return await self._get_ci_status(task)
        if call.name == "get_branch_diff":
            return await self._get_branch_diff(task)
        raise PermissionError(f"tool '{call.name}' is not implemented")
