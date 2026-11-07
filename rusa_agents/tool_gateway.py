"""Bounded repository tools and Docker-isolated check execution.

This module deliberately exposes named operations, never arbitrary shell execution.
The workspace is a staging checkout. The Docker sandbox is the execution boundary.
"""
from __future__ import annotations  # noqa: I001

import json
import os
import shutil
import stat
import subprocess  # nosec B404 - only fixed argv operations with shell=False are exposed
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from typing import Any
from uuid import uuid4

from .contracts import WorkerRole
from .metrics import TOOL_EXECUTIONS

DOCKER_BIN = shutil.which("docker")
GIT_BIN = shutil.which("git")


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
        max_snapshot_bytes: int = 100_000_000,
    ) -> None:
        self.image = image or os.getenv("RUSA_SANDBOX_IMAGE", "rusa-ai-agent-sandbox:py312")
        self.timeout_seconds = timeout_seconds
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.max_snapshot_bytes = max_snapshot_bytes

    def verify_ready(self, require_rootless: bool = False, require_image: bool = False) -> None:
        """Fail startup if the Docker CLI/daemon or required sandbox image is unavailable."""
        if not DOCKER_BIN:
            raise RuntimeError("Docker CLI is not installed in the worker image")
        try:
            info = subprocess.run(  # nosec B603 - fixed argv and absolute Docker executable
                [DOCKER_BIN, "info", "--format", "{{json .SecurityOptions}}"],
                check=False, capture_output=True, text=True, timeout=20,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Docker CLI is not installed in the worker image") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Docker daemon readiness check timed out") from exc
        if info.returncode != 0:
            raise RuntimeError(f"Docker daemon is unavailable: {info.stderr[-1000:]}")
        if require_rootless and "rootless" not in info.stdout.lower():
            raise RuntimeError("Production Docker daemon must run in rootless mode")
        if require_image:
            inspected = subprocess.run(  # nosec B603 - fixed argv and absolute Docker executable
                [DOCKER_BIN, "image", "inspect", self.image],
                check=False, capture_output=True, text=True, timeout=30,
            )
            if inspected.returncode != 0:
                raise RuntimeError(
                    "Pinned sandbox image is not present in the worker's rootless Docker daemon"
                )

    def run(self, workspace: Path, profile: str) -> ToolResult:
        if not DOCKER_BIN:
            return ToolResult(False, "Docker CLI is not installed in the worker image.")
        if profile not in self.PROFILES:
            raise ToolDenied(f"Unknown validation profile: {profile}")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ToolDenied("Sandbox workspace must be a directory")

        totals = [0]

        def bounded_copy(source: str, destination: str) -> str:
            size = Path(source).stat().st_size
            totals[0] += size
            if totals[0] > self.max_snapshot_bytes:
                raise ToolDenied("Sandbox snapshot exceeds configured size limit")
            copied = shutil.copy2(source, destination)
            mode = stat.S_IMODE(Path(copied).stat().st_mode)
            os.chmod(copied, (mode | 0o444) & ~0o222)
            return copied

        sensitive_parts = {
            ".git", ".secrets", "secrets", "credentials", ".ssh", ".aws",
            ".azure", ".docker", ".kube", ".gcloud", ".gnupg", ".config",
            ".rusa-ai", ".terraform", ".venv", "venv", "node_modules",
        }
        sensitive_names = {
            ".npmrc", ".pypirc", ".netrc", ".git-credentials",
            "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
            "credentials.json", "service-account.json", "secrets.json",
            "token.json", "kubeconfig", "application_default_credentials.json",
            ".dockerconfigjson",
        }
        sensitive_suffixes = {
            ".pem", ".key", ".p12", ".pfx", ".keystore", ".crt",
            ".cer", ".der", ".ppk", ".tfstate", ".tfvars",
        }

        def ignore_sensitive(directory: str, names: list[str]) -> set[str]:
            ignored = set()
            for name in names:
                candidate = Path(directory) / name
                lowered = name.lower()
                if (
                    candidate.is_symlink()
                    or lowered in sensitive_parts
                    or lowered in sensitive_names
                    or (lowered.startswith(".env") and lowered != ".env.example")
                    or lowered.endswith((".tfstate", ".tfstate.backup"))
                    or candidate.suffix.lower() in sensitive_suffixes
                    or lowered in {"__pycache__", ".pytest_cache", ".ruff_cache"}
                ):
                    ignored.add(name)
            return ignored
        # The real checkout and Git metadata are never mounted into test containers.
        with tempfile.TemporaryDirectory(prefix=".rusa-sandbox-", dir=workspace.parent) as temp_root:
            snapshot = Path(temp_root) / "workspace"
            try:
                shutil.copytree(
                    workspace, snapshot, symlinks=True, ignore=ignore_sensitive,
                    copy_function=bounded_copy,
                )
                for directory, _, filenames in os.walk(snapshot):
                    os.chmod(directory, 0o555)  # nosec B103 - read/traverse only; snapshot is mounted read-only in Docker
                    for filename in filenames:
                        path = Path(directory) / filename
                        if not path.is_symlink():
                            mode = stat.S_IMODE(path.stat().st_mode)
                            os.chmod(path, (mode | 0o444) & ~0o222)
            except (OSError, ToolDenied) as exc:
                return ToolResult(False, f"Could not create bounded sandbox snapshot: {exc}")

            container_name = f"rusa-sandbox-{uuid4().hex}"
            command = [
                DOCKER_BIN, "run", "--rm", "--name", container_name,
                "--network=none",
                "--read-only",
                "--user", "10001:10001",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=128",
                f"--memory={self.memory_limit}",
                f"--memory-swap={self.memory_limit}",
                f"--cpus={self.cpu_limit}",
                "--tmpfs", "/run/rusa-tmp:rw,noexec,nosuid,size=256m",  # nosec B108 - private ephemeral container tmpfs
                "--mount", f"type=bind,src={snapshot},dst=/workspace,readonly",
                "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--env", "TMPDIR=/run/rusa-tmp",
                "--env", "PYTHONPYCACHEPREFIX=/run/rusa-tmp/pycache",
                "--env", "HOME=/run/rusa-tmp",
                "--env", "XDG_CACHE_HOME=/run/rusa-tmp/.cache",
                "--env", "RUFF_CACHE_DIR=/run/rusa-tmp/.cache/ruff",
                "--env", "PYTEST_ADDOPTS=-p no:cacheprovider",
                "--workdir", "/workspace",
                self.image,
                *self.PROFILES[profile],
            ]
            try:
                completed = subprocess.run(  # nosec B603 - fixed profile argv, shell=False
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
                try:
                    subprocess.run(  # nosec B603 - fixed cleanup argv, shell=False
                        [DOCKER_BIN, "rm", "-f", container_name],
                        check=False, capture_output=True, text=True, timeout=30,
                        env={key: value for key, value in os.environ.items()
                             if key in {"PATH", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "HOME"}},
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
                return ToolResult(False, f"Sandbox profile '{profile}' exceeded {self.timeout_seconds}s timeout.")
            output = (completed.stdout + "\n" + completed.stderr)[-20000:]
            return ToolResult(
                completed.returncode == 0,
                output,
                {"profile": profile, "exit_code": completed.returncode, "image": self.image,
                 "snapshot_bytes": totals[0]},
            )


class SandboxExecutorClient:
    """Calls a narrowly scoped sandbox service; the worker never needs a Docker socket."""

    def __init__(
        self,
        url: str,
        token: str,
        image: str | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Sandbox executor URL must be credential-free HTTP(S)")
        if not isinstance(token, str) or len(token.encode("utf-8")) < 32:
            raise ValueError("Sandbox executor token must contain at least 32 UTF-8 bytes")
        self.url = url.rstrip("/")
        self.token = token
        self.image = image
        self.timeout_seconds = timeout_seconds

    def verify_ready(self, require_rootless: bool = False, require_image: bool = False) -> None:
        import httpx

        try:
            with httpx.Client(timeout=10.0, follow_redirects=False) as client:
                response = client.get(f"{self.url}/healthz")
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Sandbox executor readiness check failed: {type(exc).__name__}") from None
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            raise RuntimeError("Sandbox executor is not ready")
        if require_rootless and payload.get("rootless") is not True:
            raise RuntimeError("Production sandbox executor must use a rootless Docker daemon")
        if require_image and (not self.image or payload.get("image") != self.image):
            raise RuntimeError("Sandbox executor image does not match the configured immutable digest")

    def run(self, workspace: Path, profile: str) -> ToolResult:
        import httpx

        if profile not in DockerSandbox.PROFILES:
            raise ToolDenied(f"Unknown validation profile: {profile}")
        try:
            resolved_workspace = Path(workspace).resolve(strict=True)
        except OSError as exc:
            raise ToolDenied("Sandbox workspace is unavailable") from exc
        try:
            with httpx.Client(
                timeout=self.timeout_seconds + 45,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    f"{self.url}/v1/run",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={"workspace": str(resolved_workspace), "profile": profile},
                )
            if response.status_code in {401, 403}:
                raise ToolDenied("Sandbox executor rejected the worker capability")
            response.raise_for_status()
            payload = response.json()
        except ToolDenied:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(
                False,
                f"Sandbox executor request failed: {type(exc).__name__}",
                {"profile": profile, "executor": self.url},
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            return ToolResult(False, "Sandbox executor returned an invalid response.")
        output = payload.get("output", "")
        evidence = payload.get("evidence")
        if not isinstance(output, str):
            output = "Sandbox executor returned a non-text output."
        if not isinstance(evidence, dict):
            evidence = {"profile": profile, "exit_code": 1}
        return ToolResult(payload["ok"], output[-20000:], evidence)


class WorkspaceToolGateway:
    """Safe file operations for one already-created, isolated feature-branch checkout."""

    PROTECTED_PARTS = {
        ".git", ".env", ".env.local", ".env.production", ".secrets",
        ".ssh", ".aws", ".azure", "secrets", "credentials",
        ".gitattributes", ".gitmodules", ".lfsconfig",
    }
    PROTECTED_PREFIXES = (".github/workflows/",)
    PROTECTED_NAMES = {
        "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
        "credentials.json", "service-account.json", ".npmrc", ".pypirc",
        ".netrc", "secrets.json", "token.json", ".gitattributes", ".gitmodules", ".lfsconfig",
    }
    PROTECTED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".crt", ".cer", ".der", ".ppk", ".tfstate", ".tfvars"}
    PROTECTED_WRITE_NAMES = {
        ".env.example", ".gitignore", ".dockerignore", "requirements.txt",
        "requirements-agent-sandbox.txt", "pyproject.toml", "dockerfile",
        "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
        "containerfile", "makefile", "procfile", "serverless.yml",
        "vercel.json", "netlify.toml",
        "setup.py", "setup.cfg", "package.json", "package-lock.json",
        "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock",
        "pipfile", "pipfile.lock", "cargo.toml", "go.mod",
    }
    PROTECTED_WRITE_PREFIXES = (
        ".github/", "migrations/", "schemas/", "llm-project/configs/",
        "deploy/", "deployment/", "infra/", "infrastructure/", "terraform/",
        "k8s/", "kubernetes/", "helm/", "systemd/", ".devcontainer/",
    )

    def __init__(
        self,
        workspace: str | Path,
        sandbox: DockerSandbox | None = None,
        max_file_bytes: int = 1_000_000,
        knowledge_service: Any | None = None,
        knowledge_tenant_id: str | None = None,
        knowledge_collection: str = "agent",
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.sandbox = sandbox or DockerSandbox()
        self.max_file_bytes = max_file_bytes
        if knowledge_service is not None and (
            not isinstance(knowledge_tenant_id, str)
            or not knowledge_tenant_id.strip()
        ):
            raise ValueError("knowledge tenant identity must come from the authenticated task")
        self.knowledge_service = knowledge_service
        self.knowledge_tenant_id = knowledge_tenant_id
        self.knowledge_collection = knowledge_collection

    def __init__(
        self,
        workspace: str | Path,
        sandbox: DockerSandbox | SandboxExecutorClient | None = None,
        max_file_bytes: int = 1_000_000,
        knowledge_service: Any | None = None,
        knowledge_tenant_id: str | None = None,
        knowledge_collection: str = "agent",
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        root_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(self.workspace, root_flags)
        except OSError as exc:
            raise ValueError("workspace root must be a real, non-symlink directory") from exc
        self.sandbox = sandbox or DockerSandbox()
        self.max_file_bytes = max_file_bytes
        if knowledge_service is not None and (
            not isinstance(knowledge_tenant_id, str)
            or not knowledge_tenant_id.strip()
        ):
            os.close(self._root_fd)
            raise ValueError("knowledge tenant identity must come from the authenticated task")
        self.knowledge_service = knowledge_service
        self.knowledge_tenant_id = knowledge_tenant_id
        self.knowledge_collection = knowledge_collection

    def close(self) -> None:
        fd = getattr(self, "_root_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._root_fd = -1

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    @classmethod
    def _is_protected_path(cls, parts: tuple[str, ...]) -> bool:
        lowered = tuple(part.lower() for part in parts)
        if any(
            part in cls.PROTECTED_PARTS
            or (part.startswith(".env") and part != ".env.example")
            or part in cls.PROTECTED_NAMES
            or PurePosixPath(part).suffix.lower() in cls.PROTECTED_SUFFIXES
            for part in lowered
        ):
            return True
        normalized = "/".join(lowered)
        return any(
            normalized == prefix.rstrip("/")
            or normalized.startswith(prefix.lower())
            for prefix in cls.PROTECTED_PREFIXES
        )

    def _path_parts(self, raw_path: str, *, allow_root: bool = False) -> tuple[str, ...]:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolDenied("A non-empty relative path is required")
        if "\x00" in raw_path or raw_path.startswith("/") or "\\" in raw_path:
            raise ToolDenied("Absolute paths, traversal, and backslashes are prohibited")
        candidate = PurePosixPath(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ToolDenied("Absolute paths and traversal are prohibited")
        parts = tuple(part for part in candidate.parts if part not in {"", "."})
        if not parts and not allow_root:
            raise ToolDenied("A file path is required")
        if self._is_protected_path(parts):
            raise ToolDenied("Access to protected paths is prohibited")
        return parts

    def _open_directory_fd(self, parts: tuple[str, ...]) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.dup(self._root_fd)
        try:
            for part in parts:
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except OSError as exc:
            os.close(current_fd)
            raise ToolDenied("Path contains a missing, non-directory, or symbolic-link component") from exc

    def _open_parent_fd(
        self, parts: tuple[str, ...], *, create_parents: bool = False
    ) -> tuple[int, str]:
        if not parts:
            raise ToolDenied("A file path is required")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.dup(self._root_fd)
        try:
            for part in parts[:-1]:
                if create_parents:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd, parts[-1]
        except OSError as exc:
            os.close(current_fd)
            raise ToolDenied("Path contains a missing, non-directory, or symbolic-link component") from exc

    def _resolve(self, raw_path: str) -> Path:
        """Lexical path validation for callers that need a display path, not file access."""
        parts = self._path_parts(raw_path)
        target = self.workspace.joinpath(*parts)
        current = self.workspace
        for part in parts:
            current = current / part
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    raise ToolDenied("Symbolic links are not accessible through the agent gateway")
            except FileNotFoundError:
                break
        resolved = target.resolve(strict=False)
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ToolDenied("Path escapes the assigned workspace")
        return target

    def read_file(self, raw_path: str) -> ToolResult:
        parts = self._path_parts(raw_path)
        parent_fd, name = self._open_parent_fd(parts)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                file_fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return ToolResult(False, f"File not found: {raw_path}")
            try:
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    return ToolResult(False, f"Not a regular file: {raw_path}")
                if metadata.st_nlink > 1:
                    raise ToolDenied("Hard-linked files are not accessible through the agent gateway")
                if metadata.st_size > self.max_file_bytes:
                    raise ToolDenied("File exceeds configured read limit")
                chunks = []
                total = 0
                while True:
                    chunk = os.read(file_fd, min(65536, self.max_file_bytes + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > self.max_file_bytes:
                        raise ToolDenied("File exceeds configured read limit")
                content = b"".join(chunks).decode("utf-8")
                return ToolResult(True, content, {"path": raw_path, "bytes": total})
            finally:
                os.close(file_fd)
        except OSError as exc:
            raise ToolDenied("File could not be opened safely") from exc
        finally:
            os.close(parent_fd)

    def write_file(self, raw_path: str, content: str, role: WorkerRole) -> ToolResult:
        if role != WorkerRole.DEVELOPER:
            raise ToolDenied("Only the developer worker may modify workspace files")
        if not isinstance(content, str):
            raise ToolDenied("File content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ToolDenied("File content is invalid or exceeds configured write limit")
        parts = self._path_parts(raw_path)
        relative = "/".join(parts)
        leaf = parts[-1].lower()
        if (
            leaf in self.PROTECTED_WRITE_NAMES
            or leaf.startswith(("requirements", "dockerfile.", "docker-compose.", "compose."))
            or PurePosixPath(relative).suffix.lower() in self.PROTECTED_SUFFIXES
            or any(
                relative.lower() == prefix.rstrip("/")
                or relative.lower().startswith(prefix.lower())
                for prefix in self.PROTECTED_WRITE_PREFIXES
            )
        ):
            raise ToolDenied("Dependency, deployment, schema, and migration files require human review")
        parent_fd, name = self._open_parent_fd(parts, create_parents=True)
        temporary = f".{name}.{uuid4().hex}.tmp"
        file_fd = -1
        try:
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
            ):
                raise ToolDenied("Refusing to overwrite a symbolic link or non-regular file")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
            view = memoryview(encoded)
            while view:
                written = os.write(file_fd, view)
                view = view[written:]
            os.fsync(file_fd)
            os.close(file_fd)
            file_fd = -1
            # Both names are resolved relative to the same already-open directory.
            # Replacing a symlink replaces the link itself; it never follows its target.
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            raise ToolDenied("File could not be written safely") from exc
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        return ToolResult(
            True,
            f"Wrote {raw_path}",
            {"path": raw_path, "bytes": len(encoded)},
        )

    def list_files(self, raw_path: str = ".") -> ToolResult:
        parts = self._path_parts(raw_path, allow_root=True)
        try:
            root_fd = self._open_directory_fd(parts)
        except ToolDenied:
            return ToolResult(False, f"Directory not found: {raw_path}")
        files: list[str] = []

        def walk(directory_fd: int, prefix: tuple[str, ...]) -> None:
            if len(files) >= 500:
                return
            for name in sorted(os.listdir(directory_fd)):
                child_parts = prefix + (name,)
                if self._is_protected_path(child_parts):
                    continue
                try:
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    child_fd = self._open_directory_fd_relative(directory_fd, name)
                    try:
                        walk(child_fd, child_parts)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    files.append("/".join(child_parts))
                    if len(files) >= 500:
                        return

        try:
            walk(root_fd, parts)
        finally:
            os.close(root_fd)
        return ToolResult(True, "\n".join(files), {"count": len(files)})

    @staticmethod
    def _open_directory_fd_relative(parent_fd: int, name: str) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ToolDenied("Directory changed while listing the workspace") from exc

    def run_check(self, profile: str, role: WorkerRole) -> ToolResult:
        if role not in {WorkerRole.TESTER, WorkerRole.DEBUGGER, WorkerRole.SECURITY}:
            raise ToolDenied("Only tester, debugger, or security workers may run validation profiles")
        allowed_profiles = {
            WorkerRole.TESTER: {"pytest"},
            WorkerRole.DEBUGGER: {"pytest", "compile", "ruff"},
            WorkerRole.SECURITY: {"bandit", "ruff", "compile"},
        }
        if profile not in allowed_profiles.get(role, set()):
            raise ToolDenied(f"{role.value} worker cannot run the {profile} validation profile")
        return self.sandbox.run(self.workspace, profile)

    def git_diff(self) -> ToolResult:
        if not GIT_BIN:
            return ToolResult(False, "Git executable is unavailable in the worker image.")
        environment = {
            "PATH": os.getenv("PATH", "/usr/bin:/bin"),
            "HOME": tempfile.gettempdir(),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        names = subprocess.run(
            [GIT_BIN, "-C", str(self.workspace), "diff", "HEAD", "--no-renames",
             "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--"],
            check=False, capture_output=True, timeout=30, env=environment,
        )
        if names.returncode != 0:
            return ToolResult(False, names.stderr.decode("utf-8", "replace")[-8000:])
        safe_paths = []
        for raw in names.stdout.split(b"\0"):
            if not raw:
                continue
            path = raw.decode("utf-8", "surrogateescape")
            try:
                parts = self._path_parts(path)
            except ToolDenied:
                continue
            current = self.workspace
            unsafe = False
            for part in parts:
                current = current / part
                try:
                    if stat.S_ISLNK(os.lstat(current).st_mode):
                        unsafe = True
                        break
                except FileNotFoundError:
                    continue
            if not unsafe:
                safe_paths.append(path)
        if not safe_paths:
            return ToolResult(True, "", {"exit_code": 0, "filtered_paths": True})
        completed = subprocess.run(
            [GIT_BIN, "-C", str(self.workspace), "diff", "HEAD", "--no-renames",
             "--no-ext-diff", "--no-textconv", "--", *sorted(set(safe_paths))],
            check=False, capture_output=True, text=True, timeout=30, env=environment,
        )
        if completed.returncode != 0:
            return ToolResult(False, completed.stderr[-8000:])
        return ToolResult(True, completed.stdout[-20000:], {"exit_code": completed.returncode})

    def _knowledge_context(self) -> tuple[Any, str]:
        if self.knowledge_service is None or not self.knowledge_tenant_id:
            raise ToolDenied("Tenant-scoped knowledge retrieval is not enabled for this worker")
        return self.knowledge_service, self.knowledge_tenant_id

    def _search_knowledge(self, args: dict[str, Any]) -> ToolResult:
        service, tenant_id = self._knowledge_context()
        query = args.get("query")
        collection = args.get("collection", self.knowledge_collection)
        limit = args.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ToolDenied("Knowledge query must be a non-empty string of at most 2000 characters")
        if not isinstance(collection, str) or not collection:
            raise ToolDenied("Knowledge collection is invalid")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ToolDenied("Knowledge result limit must be between 1 and 10")
        results = service.search(
            tenant_id=tenant_id, collection=collection, query=query, limit=limit
        )
        output = json.dumps(
            {"query": query, "collection": collection, "results": results},
            ensure_ascii=False,
            default=str,
        )[:12_000]
        return ToolResult(True, output, {"source": "tenant_knowledge", "collection": collection})

    def _get_knowledge_document(self, args: dict[str, Any]) -> ToolResult:
        service, tenant_id = self._knowledge_context()
        document_id = args.get("document_id")
        collection = args.get("collection", self.knowledge_collection)
        if not isinstance(document_id, str) or not document_id or len(document_id) > 64:
            raise ToolDenied("Knowledge document ID is invalid")
        if not isinstance(collection, str) or not collection:
            raise ToolDenied("Knowledge collection is invalid")
        document = service.get_document(
            tenant_id=tenant_id,
            collection=collection,
            document_id=document_id,
            max_chars=12_000,
        )
        output = json.dumps(
            {"found": document is not None, "document": document},
            ensure_ascii=False,
            default=str,
        )[:12_500]
        return ToolResult(True, output, {"source": "tenant_knowledge", "collection": collection})

    def _list_knowledge_collections(self) -> ToolResult:
        service, tenant_id = self._knowledge_context()
        collections = service.list_collections(tenant_id=tenant_id)
        return ToolResult(
            True,
            json.dumps({"collections": collections}, ensure_ascii=False, default=str)[:4000],
            {"source": "tenant_knowledge"},
        )

    def execute(self, action: dict[str, Any], role: WorkerRole) -> ToolResult:
        name = action.get("name")
        args = action.get("arguments", {})
        if not isinstance(args, dict):
            raise ToolDenied("Tool arguments must be an object")
        schemas = {
            "list_files": {"path"},
            "read_file": {"path"},
            "write_file": {"path", "content"},
            "run_check": {"profile"},
            "git_diff": set(),
            "search_knowledge": {"query", "collection", "limit"},
            "get_document": {"document_id", "collection"},
            "list_collections": set(),
        }
        if name not in schemas:
            TOOL_EXECUTIONS.labels(str(name)[:64], "denied").inc()
            raise ToolDenied(f"Tool '{name}' is not registered")
        if set(args) - schemas[name]:
            TOOL_EXECUTIONS.labels(name, "denied").inc()
            raise ToolDenied(f"Unexpected arguments for tool '{name}'")
        try:
            if name == "list_files":
                result = self.list_files(args.get("path", "."))
            elif name == "read_file":
                result = self.read_file(args.get("path", ""))
            elif name == "write_file":
                result = self.write_file(args.get("path", ""), args.get("content", ""), role)
            elif name == "run_check":
                result = self.run_check(args.get("profile", ""), role)
            elif name == "search_knowledge":
                result = self._search_knowledge(args)
            elif name == "get_document":
                result = self._get_knowledge_document(args)
            elif name == "list_collections":
                result = self._list_knowledge_collections()
            elif name == "git_diff":
                result = self.git_diff()
            else:
                raise ToolDenied(f"Tool '{name}' is not registered")
            TOOL_EXECUTIONS.labels(name, "success" if result.ok else "failure").inc()
            return result
        except ToolDenied:
            TOOL_EXECUTIONS.labels(name, "denied").inc()
            raise
        raise ToolDenied(f"Tool '{name}' is not registered")
