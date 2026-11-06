"""Resource-bounded Docker sandbox adapter for untrusted repository commands.

This is defense-in-depth, not a substitute for a dedicated ephemeral VM/runner.
Production use requires a rootless Docker daemon with gVisor (runsc) installed.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


_IMAGE_DIGEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*@sha256:[a-fA-F0-9]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[a-fA-F0-9]{64}$")


class SandboxError(RuntimeError):
    """Raised when a command cannot safely be started."""


@dataclass(frozen=True)
class SandboxResult:
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool


class DockerSandbox:
    def __init__(
        self,
        workspace_root: str,
        image: str,
        timeout_seconds: float = 120,
        memory_limit: str = "2g",
        cpu_limit: float = 2.0,
        max_output_bytes: int = 1_000_000,
        docker_binary: str = "docker",
        sandbox_uid: int = 10001,
        sandbox_gid: int = 10001,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        if not _IMAGE_DIGEST_RE.fullmatch(image):
            raise ValueError("sandbox image must be pinned by an immutable sha256 digest")
        if (
            not math.isfinite(timeout_seconds)
            or not math.isfinite(cpu_limit)
            or timeout_seconds <= 0
            or cpu_limit <= 0
            or max_output_bytes <= 0
        ):
            raise ValueError("sandbox resource limits must be finite and positive")
        if not re.fullmatch(r"[1-9][0-9]*(m|g)", memory_limit):
            raise ValueError("memory_limit must be a positive value such as 512m or 2g")
        if not docker_binary or "\x00" in docker_binary:
            raise ValueError("invalid Docker executable")
        if sandbox_uid <= 0 or sandbox_gid <= 0:
            raise ValueError("sandbox UID/GID must be positive non-root IDs")
        self.workspace_root = root
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.max_output_bytes = max_output_bytes
        self.docker_binary = docker_binary
        self.sandbox_uid = sandbox_uid
        self.sandbox_gid = sandbox_gid

    @staticmethod
    def _validate_runtime_info(security_options_json: str, runtimes_json: str) -> None:
        try:
            security_options = json.loads(security_options_json)
            runtimes = json.loads(runtimes_json)
        except json.JSONDecodeError as exc:
            raise SandboxError("Docker runtime preflight returned invalid metadata") from exc
        rootless = (
            isinstance(security_options, list)
            and any("rootless" in str(item).lower() for item in security_options)
        )
        if not rootless:
            raise SandboxError("sandbox requires a rootless Docker daemon")
        if not isinstance(runtimes, dict) or "runsc" not in runtimes:
            raise SandboxError("sandbox requires the gVisor runsc runtime")

    async def _docker_info(self, format_string: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker_binary,
                "info",
                "--format",
                format_string,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError) as exc:
            raise SandboxError("Docker runtime preflight failed") from exc
        if process.returncode != 0:
            raise SandboxError("Docker runtime is unavailable")
        return stdout.decode("utf-8", errors="replace").strip()

    async def _verify_runtime(self) -> None:
        security_options = await self._docker_info("{{json .SecurityOptions}}")
        runtimes = await self._docker_info("{{json .Runtimes}}")
        self._validate_runtime_info(security_options, runtimes)

    def _validate_workspace(self, workspace: str) -> Path:
        supplied = Path(workspace).expanduser()
        if supplied.is_symlink():
            raise SandboxError("workspace symlinks are not allowed")
        resolved = supplied.resolve(strict=True)
        if not resolved.is_dir() or resolved == self.workspace_root:
            raise SandboxError("workspace must be a child directory of workspace_root")
        if self.workspace_root not in resolved.parents:
            raise SandboxError("workspace escapes the configured workspace_root")
        if "," in str(resolved) or "\n" in str(resolved):
            raise SandboxError("workspace path contains unsupported mount characters")
        return resolved

    def _build_command(
        self,
        workspace: Path,
        command: list[str],
        cid_file: str | None = None,
    ) -> list[str]:
        if not command or len(command) > 256:
            raise ValueError("command must contain between 1 and 256 arguments")
        if any(
            not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 4096
            for arg in command
        ):
            raise ValueError("command arguments must be non-empty strings of at most 4096 chars")

        mount = f"type=bind,src={workspace},dst=/workspace,rw"
        argv = [
            self.docker_binary,
            "run",
            "--rm",
            "--pull=never",
            "--init",
            "--runtime=runsc",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=128",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpu_limit}",
            f"--user={self.sandbox_uid}:{self.sandbox_gid}",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            "--shm-size=16m",
            "--ulimit=nofile=1024:1024",
        ]
        if cid_file:
            argv.append(f"--cidfile={cid_file}")
        argv.extend(
            [
                f"--mount={mount}",
                "--workdir=/workspace",
                "--env=HOME=/tmp",
                "--env=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                self.image,
                *command,
            ]
        )
        return argv

    async def _read_bounded(self, stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        output = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            remaining = self.max_output_bytes - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(output), truncated

    async def _kill_container(self, cid_file: Path) -> None:
        try:
            container_id = cid_file.read_text(encoding="ascii").strip()
        except OSError:
            return
        if not _CONTAINER_ID_RE.fullmatch(container_id):
            return

        for action in ("kill", "rm"):
            argv = [self.docker_binary, action]
            if action == "rm":
                argv.append("-f")
            argv.append(container_id)
            cleanup = None
            try:
                cleanup = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(cleanup.wait(), timeout=5)
            except (OSError, asyncio.TimeoutError):
                if cleanup is not None and cleanup.returncode is None:
                    cleanup.kill()
                    await cleanup.wait()

    async def run(self, workspace: str, command: list[str]) -> SandboxResult:
        resolved = self._validate_workspace(workspace)
        await self._verify_runtime()
        with tempfile.TemporaryDirectory(prefix="rusa-sandbox-") as temp_dir:
            cid_file = Path(temp_dir) / "container.id"
            argv = self._build_command(resolved, command, str(cid_file))
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise SandboxError("sandbox runtime could not be started") from exc

            stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
            stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                await self._kill_container(cid_file)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            except asyncio.CancelledError:
                await self._kill_container(cid_file)
                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise

            stdout_data, stdout_truncated = await stdout_task
            stderr_data, stderr_truncated = await stderr_task
            return SandboxResult(
                return_code=process.returncode if process.returncode is not None else -1,
                stdout=stdout_data.decode("utf-8", errors="replace"),
                stderr=stderr_data.decode("utf-8", errors="replace"),
                timed_out=timed_out,
                output_truncated=stdout_truncated or stderr_truncated,
            )
