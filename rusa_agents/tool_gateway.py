"""Bounded repository tools and Docker-isolated check execution.

This module deliberately exposes named operations, never arbitrary shell execution.
The workspace is a staging checkout. The Docker sandbox is the execution boundary.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import WorkerRole


class ToolDenied(PermissionError):
    """Raised when a worker attempts an operation outside its role policy."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    evidence: dict[str, Any] | None = None


class DockerSandbox:
    """Runs only predefined validation profiles in a network-disabled container."""

    PROFILES = {
        "pytest": ["python", "-m", "pytest", "-q"],
        "compile": ["python", "-m", "compileall", "-q", "rusa_agents", "llm-project/serve", "tests"],
        "ruff": ["ruff", "check", "rusa_agents", "tests", "llm-project/serve"],
        "bandit": ["bandit", "-q", "-r", "rusa_agents"],
    }

    def __init__(
        self,
        image: str | None = None,
        timeout_seconds: int = 600,
        memory_limit: str = "2g",
        cpu_limit: str = "2",
    ) -> None:
        self.image = image or os.getenv("RUSA_SANDBOX_IMAGE", "rusa-ai-agent-sandbox:py310")
        self.timeout_seconds = timeout_seconds
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit

    def run(self, workspace: Path, profile: str) -> ToolResult:
        if profile not in self.PROFILES:
            raise ToolDenied(f"Unknown validation profile: {profile}")
        workspace = workspace.resolve(strict=True)
        command = [
            "docker", "run", "--rm",
            "--network=none",
            "--read-only",
            "--user", "10001:10001",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=128",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpu_limit}",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--mount", f"type=bind,src={workspace},dst=/workspace,rw",
            "--workdir", "/workspace",
            self.image,
            *self.PROFILES[profile],
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env={key: value for key, value in os.environ.items()
                     if key in {"PATH", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "HOME"}},
            )
        except FileNotFoundError as exc:
            return ToolResult(False, "Docker executable is unavailable; sandbox checks cannot run.", {"error": str(exc)})
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"Sandbox profile '{profile}' exceeded {self.timeout_seconds}s timeout.")
        output = (completed.stdout + "\n" + completed.stderr)[-20000:]
        return ToolResult(
            completed.returncode == 0,
            output,
            {"profile": profile, "exit_code": completed.returncode, "image": self.image},
        )


class WorkspaceToolGateway:
    """Safe file operations for one already-created, isolated feature-branch checkout."""

    PROTECTED_PARTS = {
        ".git", ".env", ".env.local", ".env.production", ".secrets",
        ".ssh", ".aws", ".azure", "secrets", "credentials",
    }
    PROTECTED_PREFIXES = (".github/workflows/",)

    def __init__(
        self,
        workspace: str | Path,
        sandbox: DockerSandbox | None = None,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.sandbox = sandbox or DockerSandbox()
        self.max_file_bytes = max_file_bytes

    def _resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolDenied("A non-empty relative path is required")
        candidate = PurePosixPath(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in raw_path:
            raise ToolDenied("Absolute paths, traversal, and backslashes are prohibited")
        if any(part.lower() in self.PROTECTED_PARTS for part in candidate.parts):
            raise ToolDenied("Access to protected paths is prohibited")
        normalized = candidate.as_posix()
        if any(normalized.startswith(prefix) for prefix in self.PROTECTED_PREFIXES):
            raise ToolDenied("CI workflow files require a separate human-reviewed change")
        target = self.workspace.joinpath(*candidate.parts)
        current = self.workspace
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ToolDenied("Symbolic links are not accessible through the agent gateway")
        resolved = target.resolve(strict=False)
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ToolDenied("Path escapes the assigned workspace")
        if any(part.lower() in self.PROTECTED_PARTS for part in resolved.relative_to(self.workspace).parts):
            raise ToolDenied("Resolved path points to protected content")
        return target

    def read_file(self, raw_path: str) -> ToolResult:
        target = self._resolve(raw_path)
        if not target.is_file():
            return ToolResult(False, f"File not found: {raw_path}")
        if target.stat().st_size > self.max_file_bytes:
            raise ToolDenied("File exceeds configured read limit")
        return ToolResult(True, target.read_text(encoding="utf-8"), {"path": raw_path})

    def write_file(self, raw_path: str, content: str, role: WorkerRole) -> ToolResult:
        if role != WorkerRole.DEVELOPER:
            raise ToolDenied("Only the developer worker may modify workspace files")
        if not isinstance(content, str) or len(content.encode("utf-8")) > self.max_file_bytes:
            raise ToolDenied("File content is invalid or exceeds configured write limit")
        target = self._resolve(raw_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(True, f"Wrote {raw_path}", {"path": raw_path, "bytes": len(content.encode("utf-8"))})

    def list_files(self, raw_path: str = ".") -> ToolResult:
        target = self.workspace if raw_path == "." else self._resolve(raw_path)
        if not target.exists() or not target.is_dir():
            return ToolResult(False, f"Directory not found: {raw_path}")
        files = []
        for item in sorted(target.rglob("*")):
            if len(files) >= 500:
                break
            try:
                rel = item.relative_to(self.workspace).as_posix()
            except ValueError:
                continue
            if any(part.lower() in self.PROTECTED_PARTS for part in PurePosixPath(rel).parts):
                continue
            if any(rel.startswith(prefix) for prefix in self.PROTECTED_PREFIXES):
                continue
            if item.is_file() and not item.is_symlink():
                files.append(rel)
        return ToolResult(True, "\n".join(files), {"count": len(files)})

    def run_check(self, profile: str, role: WorkerRole) -> ToolResult:
        if role not in {WorkerRole.TESTER, WorkerRole.DEBUGGER, WorkerRole.SECURITY}:
            raise ToolDenied("Only tester, debugger, or security workers may run validation profiles")
        if role == WorkerRole.SECURITY and profile not in {"bandit", "ruff", "compile"}:
            raise ToolDenied("Security worker may only run static validation profiles")
        return self.sandbox.run(self.workspace, profile)

    def git_diff(self) -> ToolResult:
        completed = subprocess.run(
            ["git", "-C", str(self.workspace), "diff", "--no-ext-diff", "--"],
            check=False, capture_output=True, text=True, timeout=30,
        )
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr[-8000:])
        return ToolResult(True, completed.stdout[-20000:], {"exit_code": completed.returncode})

    def execute(self, action: dict[str, Any], role: WorkerRole) -> ToolResult:
        name = action.get("name")
        args = action.get("arguments", {})
        if not isinstance(args, dict):
            raise ToolDenied("Tool arguments must be an object")
        if name == "list_files":
            return self.list_files(str(args.get("path", ".")))
        if name == "read_file":
            return self.read_file(str(args.get("path", "")))
        if name == "write_file":
            return self.write_file(str(args.get("path", "")), args.get("content", ""), role)
        if name == "run_check":
            return self.run_check(str(args.get("profile", "")), role)
        if name == "git_diff":
            if role not in {WorkerRole.ANALYST, WorkerRole.ARCHITECT, WorkerRole.DEBUGGER, WorkerRole.TESTER, WorkerRole.SECURITY}:
                raise ToolDenied("Role cannot inspect Git diff")
            return self.git_diff()
        raise ToolDenied(f"Tool '{name}' is not registered")
