"""Bounded repository tools and Docker-isolated check execution.

This module deliberately exposes named operations, never arbitrary shell execution.
The workspace is a staging checkout. The Docker sandbox is the execution boundary.
"""
from __future__ import annotations  # noqa: I001

import hashlib
import json
import os
import shutil
import stat
import subprocess  # nosec B404 - only fixed argv operations with shell=False are exposed
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
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
    retryable: bool = False
    failure_kind: str | None = None


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

    @staticmethod
    def _snapshot_digest(snapshot: Path) -> str:
        """Hash the exact source snapshot mounted into the sandbox."""
        digest = hashlib.sha256()
        for path in sorted(snapshot.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(snapshot).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def run(self, workspace: Path, profile: str) -> ToolResult:
        if not DOCKER_BIN:
            return ToolResult(
                False,
                "Docker CLI is not installed in the worker image.",
                {"profile": profile, "error": "docker_unavailable"},
                failure_kind="infrastructure",
            )
        if profile not in self.PROFILES:
            raise ToolDenied(f"Unknown validation profile: {profile}")
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ToolDenied("Sandbox workspace must be a directory")

        execution_id = uuid4().hex
        started_at = datetime.now(timezone.utc).isoformat()
        started_clock = time.monotonic()
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
            ".azure", ".rusa-ai", ".terraform", ".venv", "venv", "node_modules",
        }
        sensitive_names = {
            ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
            "authorized_keys", "known_hosts", "credentials.json",
            "service-account.json", "secrets.json", "token.json",
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
                return ToolResult(
                    False,
                    f"Could not create bounded sandbox snapshot: {exc}",
                    {"profile": profile, "error": "snapshot_failed"},
                    failure_kind="infrastructure",
                )

            workspace_digest = self._snapshot_digest(snapshot)
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
                timeout_output = (exc.stdout or b"") + b"\\n" + (exc.stderr or b"")
                if isinstance(timeout_output, bytes):
                    timeout_output = timeout_output.decode("utf-8", errors="replace")
                return ToolResult(
                    False,
                    f"Sandbox profile '{profile}' exceeded {self.timeout_seconds}s timeout. {timeout_output[-4000:]}",
                    {
                        "profile": profile,
                        "error": "timeout",
                        "workspace_digest": workspace_digest,
                        "output_digest": hashlib.sha256(timeout_output.encode("utf-8", errors="replace")).hexdigest(),
                        "execution_id": execution_id,
                        "started_at": started_at,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "duration_seconds": round(time.monotonic() - started_clock, 6),
                    },
                    retryable=True,
                    failure_kind="infrastructure",
                )
            full_output = (completed.stdout or "") + "\n" + (completed.stderr or "")
            docker_runtime_failure = completed.returncode in {125, 126, 127}
            return ToolResult(
                completed.returncode == 0,
                full_output[-20000:],
                {
                    "profile": profile,
                    "exit_code": completed.returncode,
                    "image": self.image,
                    "snapshot_bytes": totals[0],
                    "workspace_digest": workspace_digest,
                    "output_digest": hashlib.sha256(full_output.encode("utf-8", errors="replace")).hexdigest(),
                    "execution_id": execution_id,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_seconds": round(time.monotonic() - started_clock, 6),
                    "error": "docker_runtime" if docker_runtime_failure else None,
                },
                retryable=completed.returncode == 125,
                failure_kind="infrastructure" if docker_runtime_failure else None,
            )


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
    PROTECTED_WRITE_PATHS = {
        "rusa_agents/audit.py", "rusa_agents/autonomous.py",
        "rusa_agents/contracts.py", "rusa_agents/git_workspace.py",
        "rusa_agents/memory.py", "rusa_agents/orchestrator.py",
        "rusa_agents/policy.py", "rusa_agents/queue.py",
        "rusa_agents/runtime.py", "rusa_agents/tool_gateway.py",
    }
    PROTECTED_WRITE_PREFIXES = (
        ".github/", "tests/", "test/", "qa/", "rusa_agents/security/",
        "migrations/", "schemas/", "llm-project/configs/",
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
        task_id: str | None = None,
        attempt: int = 1,
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
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise ValueError("task identity must be a non-empty string")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("task attempt must be a positive integer")
        self.knowledge_service = knowledge_service
        self.knowledge_tenant_id = knowledge_tenant_id
        self.knowledge_collection = knowledge_collection
        self.task_id = task_id
        self.attempt = attempt

    def _resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolDenied("A non-empty relative path is required")
        candidate = PurePosixPath(raw_path)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in raw_path:
            raise ToolDenied("Absolute paths, traversal, and backslashes are prohibited")
        if any(
            part.lower() in self.PROTECTED_PARTS
            or (part.lower().startswith(".env") and part.lower() != ".env.example")
            for part in candidate.parts
        ):
            raise ToolDenied("Access to protected paths is prohibited")
        if any(part.lower() in self.PROTECTED_NAMES for part in candidate.parts):
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
            or (part.lower().startswith(".env") and part.lower() != ".env.example")
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
            relative.lower() in self.PROTECTED_WRITE_PATHS
            or target.name.lower() in self.PROTECTED_WRITE_NAMES
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
        result = self.sandbox.run(self.workspace, profile)
        evidence = dict(result.evidence or {})
        evidence.update({
            "profile": profile,
            "worker_role": role.value,
            "attempt": self.attempt,
        })
        if self.task_id is not None:
            evidence["task_id"] = self.task_id
        return ToolResult(result.ok, result.output, evidence, result.retryable, result.failure_kind)

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
