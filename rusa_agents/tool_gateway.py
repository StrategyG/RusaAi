"""Bounded repository tools and Docker-isolated check execution.

This module deliberately exposes named operations, never arbitrary shell execution.
The workspace is a staging checkout. The Docker sandbox is the execution boundary.
"""
from __future__ import annotations  # noqa: I001

import os
import selectors
import shutil
import stat
import subprocess  # nosec B404 - only fixed argv operations with shell=False are exposed
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .contracts import WorkerRole
from .metrics import TOOL_EXECUTIONS

DOCKER_BIN = shutil.which("docker")
GIT_BIN = shutil.which("git")


def _is_env_file_name(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env.example":
        return False
    return lowered == ".env" or lowered.startswith(".env.") or ".env." in lowered or lowered.endswith(".env")


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
        "bandit": ["bandit", "-q", "-r", "rusa_agents", "llm-project/serve"],
    }

    def __init__(
        self,
        image: str | None = None,
        timeout_seconds: int = 600,
        memory_limit: str = "2g",
        cpu_limit: str = "2",
        max_snapshot_bytes: int = 100_000_000,
        max_snapshot_files: int = 50_000,
        max_output_bytes: int = 200_000,
    ) -> None:
        self.image = image or os.getenv("RUSA_SANDBOX_IMAGE", "rusa-ai-agent-sandbox:py312")
        self.timeout_seconds = timeout_seconds
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_snapshot_files = max_snapshot_files
        if not 1_024 <= max_output_bytes <= 5_000_000:
            raise ValueError("max_output_bytes must be between 1024 and 5000000")
        self.max_output_bytes = max_output_bytes

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
        if require_rootless:
            try:
                docker_gid = int(os.getenv("RUSA_DOCKER_GID", ""))
            except ValueError as exc:
                raise RuntimeError("Production RUSA_DOCKER_GID must be a valid numeric group ID") from exc
            if not 1 <= docker_gid <= 65535:
                raise RuntimeError("Production RUSA_DOCKER_GID must be between 1 and 65535")
            if docker_gid not in os.getgroups() and docker_gid != os.getgid():
                raise RuntimeError(
                    "Worker process is not a member of the rootless Docker socket group"
                )
        if require_image:
            inspected = subprocess.run(  # nosec B603 - fixed argv and absolute Docker executable
                [DOCKER_BIN, "image", "inspect", self.image],
                check=False, capture_output=True, text=True, timeout=30,
            )
            if inspected.returncode != 0:
                raise RuntimeError(
                    "Pinned sandbox image is not present in the worker's rootless Docker daemon"
                )

    @staticmethod
    def _run_bounded(
        command: list[str],
        timeout_seconds: int,
        env: dict[str, str],
        max_output_bytes: int,
    ) -> tuple[int | None, str, str, int]:
        """Stream Docker output with a hard memory/output budget."""
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=0,
        )
        if process.stdout is None:
            process.kill()
            raise RuntimeError("Could not capture sandbox output")
        output = bytearray()
        total_bytes = 0
        outcome = "completed"
        deadline = time.monotonic() + timeout_seconds
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = "timeout"
                    break
                events = selector.select(min(remaining, 0.25))
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total_bytes += len(chunk)
                    output.extend(chunk)
                    if len(output) > max_output_bytes:
                        del output[:len(output) - max_output_bytes]
                    if total_bytes > max_output_bytes:
                        outcome = "output_limit"
                        break
                if outcome != "completed":
                    break
        if outcome == "completed":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
        if outcome != "completed":
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        else:
            process.wait()
        process.stdout.close()
        return process.returncode, output.decode("utf-8", "replace"), outcome, total_bytes

    @staticmethod
    def _remove_container(container_name: str, env: dict[str, str]) -> None:
        if not DOCKER_BIN:
            return
        try:
            subprocess.run(
                [DOCKER_BIN, "rm", "-f", container_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def run(self, workspace: Path, profile: str) -> ToolResult:
        if not DOCKER_BIN:
            return ToolResult(False, "Docker CLI is not installed in the worker image.")
        if profile not in self.PROFILES:
            raise ToolDenied(f"Unknown validation profile: {profile}")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ToolDenied("Sandbox workspace must be a directory")

        totals = [0]
        file_count = [0]

        def bounded_copy(source: str, destination: str) -> str:
            file_count[0] += 1
            if file_count[0] > self.max_snapshot_files:
                raise ToolDenied("Sandbox snapshot exceeds configured file-count limit")
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
            ".azure", ".rusa-ai", ".terraform", ".venv", "venv", "node_modules",
        }
        sensitive_names = {
            ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
            "authorized_keys", "known_hosts", "credentials.json",
            "service-account.json", "secrets.json", "token.json",
            "secrets.yaml", "secrets.yml", "secret.yaml", "secret.yml",
            "credentials.yaml", "credentials.yml",
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
                    or _is_env_file_name(lowered)
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
                # Rootless Docker runs as a host user. Make the temporary parent
                # traversable by the socket group while keeping the snapshot read-only.
                os.chmod(temp_root, 0o750)
                docker_gid = os.getenv("RUSA_DOCKER_GID")
                if docker_gid:
                    try:
                        numeric_gid = int(docker_gid)
                    except ValueError as exc:
                        raise ToolDenied("RUSA_DOCKER_GID must be numeric") from exc
                    if not 1 <= numeric_gid <= 65535:
                        raise ToolDenied("RUSA_DOCKER_GID is outside the supported group-ID range")
                    os.chown(temp_root, -1, numeric_gid)
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
            env = {
                key: value for key, value in os.environ.items()
                if key in {"PATH", "DOCKER_CONFIG", "DOCKER_HOST", "HOME"}
            }
            try:
                exit_code, output, outcome, output_bytes = self._run_bounded(
                    command,
                    timeout_seconds=self.timeout_seconds,
                    env=env,
                    max_output_bytes=self.max_output_bytes,
                )
            except FileNotFoundError as exc:
                return ToolResult(
                    False,
                    "Docker executable is unavailable; sandbox checks cannot run.",
                    {"error": str(exc)},
                )
            except OSError as exc:
                return ToolResult(False, f"Sandbox process could not start: {exc}")

            if outcome != "completed":
                self._remove_container(container_name, env)
                if outcome == "timeout":
                    message = f"Sandbox profile '{profile}' exceeded {self.timeout_seconds}s timeout."
                else:
                    message = (
                        f"Sandbox profile '{profile}' exceeded the "
                        f"{self.max_output_bytes}-byte output limit."
                    )
                return ToolResult(
                    False,
                    message + "\n" + output[-20_000:],
                    {
                        "profile": profile,
                        "outcome": outcome,
                        "output_bytes": output_bytes,
                        "max_output_bytes": self.max_output_bytes,
                        "image": self.image,
                    },
                )
            return ToolResult(
                exit_code == 0,
                output[-20_000:],
                {
                    "profile": profile,
                    "exit_code": exit_code,
                    "image": self.image,
                    "snapshot_bytes": totals[0],
                    "output_bytes": output_bytes,
                },
            )


class WorkspaceToolGateway:
    """Safe file operations for one already-created, isolated feature-branch checkout."""

    PROTECTED_PARTS = {
        ".git", ".env", ".env.local", ".env.production", ".secrets",
        ".ssh", ".aws", ".azure", "secrets", "credentials",
        ".gitattributes", ".gitmodules", ".lfsconfig",
        ".terraform", ".venv", "venv", "node_modules",
        "__pycache__", ".pytest_cache", ".ruff_cache",
    }
    PROTECTED_PREFIXES = (".github/workflows/",)
    PROTECTED_NAMES = {
        "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
        "credentials.json", "service-account.json", ".npmrc", ".pypirc",
        ".netrc", "secrets.json", "token.json", ".gitattributes", ".gitmodules", ".lfsconfig",
        "kubeconfig", "serviceaccount.json", ".dockerconfigjson",
    }
    PROTECTED_SUFFIXES = {
        ".pem", ".key", ".p12", ".pfx", ".keystore", ".crt", ".cer", ".der",
        ".ppk", ".tfstate", ".tfvars", ".kubeconfig", ".secret", ".secrets",
    }
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
        if any(
            part.lower() in self.PROTECTED_PARTS
            or _is_env_file_name(part)
            for part in candidate.parts
        ):
            raise ToolDenied("Access to protected paths is prohibited")
        if any(
            part.lower() in self.PROTECTED_NAMES
            or part.lower().startswith(("secret.", "secrets.", "credential.", "credentials."))
            or part.lower().endswith((".secret", ".secrets"))
            for part in candidate.parts
        ):
            raise ToolDenied("Access to credential-like files is prohibited")
        if any(PurePosixPath(part).suffix.lower() in self.PROTECTED_SUFFIXES for part in candidate.parts):
            raise ToolDenied("Access to private-key and certificate files is prohibited")
        normalized = candidate.as_posix()
        if any(normalized.lower().startswith(prefix.lower()) for prefix in self.PROTECTED_PREFIXES):
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
        if any(
            part.lower() in self.PROTECTED_PARTS
            or _is_env_file_name(part)
            for part in resolved.relative_to(self.workspace).parts
        ):
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
        relative = target.relative_to(self.workspace).as_posix()
        if (
            target.name.lower() in self.PROTECTED_WRITE_NAMES
            or target.name.lower().startswith(
                ("requirements", "dockerfile.", "docker-compose.", "compose.")
            )
            or PurePosixPath(relative).suffix.lower() in self.PROTECTED_SUFFIXES
            or any(relative.lower().startswith(prefix.lower()) for prefix in self.PROTECTED_WRITE_PREFIXES)
        ):
            raise ToolDenied("Dependency, deployment, schema, and migration files require human review")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return ToolResult(
            True,
            f"Wrote {raw_path}",
            {"path": raw_path, "bytes": len(content.encode("utf-8"))},
        )

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
            parts = {part.lower() for part in PurePosixPath(rel).parts}
            if any(
                part in self.PROTECTED_PARTS
                or (part.startswith(".env") and part != ".env.example")
                or part in self.PROTECTED_NAMES
                or part.startswith(("secret.", "secrets.", "credential.", "credentials."))
                or part.endswith((".secret", ".secrets"))
                for part in parts
            ):
                continue
            if PurePosixPath(rel).suffix.lower() in self.PROTECTED_SUFFIXES:
                continue
            if any(rel.lower().startswith(prefix.lower()) for prefix in self.PROTECTED_PREFIXES):
                continue
            if item.is_file() and not item.is_symlink():
                files.append(rel)
        return ToolResult(True, "\n".join(files), {"count": len(files)})

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
        completed = subprocess.run(  # nosec B603 - fixed argv, absolute executable, shell=False
            [GIT_BIN, "-C", str(self.workspace), "diff", "--no-ext-diff", "--no-textconv", "--"],
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
        schemas = {
            "list_files": {"path"},
            "read_file": {"path"},
            "write_file": {"path", "content"},
            "run_check": {"profile"},
            "git_diff": set(),
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
            else:
                result = self.git_diff()
            TOOL_EXECUTIONS.labels(name, "success" if result.ok else "failure").inc()
            return result
        except ToolDenied:
            TOOL_EXECUTIONS.labels(name, "denied").inc()
            raise
        raise ToolDenied(f"Tool '{name}' is not registered")
