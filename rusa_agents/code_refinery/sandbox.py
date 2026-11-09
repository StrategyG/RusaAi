"""Hybrid Docker + remote Firecracker/MicroVM sandbox routing.

Docker handles ordinary validation. High-risk dynamic tests require the separately
operated MicroVM supervisor; absence or uncertainty fails closed (no Docker fallback).
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..metrics import REFINERY_SANDBOX_DURATION, REFINERY_SANDBOX_RUNS
from ..tool_gateway import DockerSandbox, ToolResult

MICROVM_PROFILES = frozenset({"pytest", "compile", "ruff", "bandit"})
MAX_FILES = 2_000
MAX_FILE_BYTES = 1_000_000
MAX_SNAPSHOT_BYTES = 100_000_000
MAX_RESPONSE_BYTES = 1_000_000
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "dist", "build",
    ".tox", "__pycache__", ".pytest_cache", ".ruff_cache",
}
SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".npmrc", ".pypirc", ".netrc",
    "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa", "private_key", "privatekey",
    "authorized_keys", "known_hosts", "credentials.json", "service-account.json",
    "secrets.json", "token.json", "terraform.tfstate",
}
SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".crt", ".cer",
    ".der", ".ppk", ".tfstate", ".tfvars",
}
DANGEROUS_CALLS = {
    "eval", "exec", "__import__", "os.system", "os.popen", "subprocess.Popen",
    "pickle.loads", "marshal.loads", "ctypes.CDLL", "ctypes.PyDLL",
    "multiprocessing.Process", "socket.socket",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


class SandboxRiskClassifier:
    """Conservative static classifier for dynamic test execution."""

    def classify(self, workspace: str | Path, profile: str) -> tuple[str, list[str]]:
        if profile != "pytest":
            return "standard", []
        root = Path(workspace).resolve(strict=True)
        reasons: list[str] = []
        inspected = 0
        total_source_bytes = 0
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            for name in files:
                path = Path(current) / name
                if path.is_symlink():
                    continue
                if path.suffix.lower() in {".so", ".dll", ".dylib", ".exe", ".whl"}:
                    reasons.append(f"native_or_binary_artifact:{path.relative_to(root).as_posix()}")
                    continue
                if path.suffix.lower() != ".py":
                    continue
                inspected += 1
                source_size = path.stat().st_size
                total_source_bytes += source_size
                if (
                    inspected > MAX_FILES
                    or source_size > MAX_FILE_BYTES
                    or total_source_bytes > MAX_SNAPSHOT_BYTES
                ):
                    reasons.append("source_inventory_exceeds_static_classifier_limits")
                    return "high", sorted(set(reasons))
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (SyntaxError, UnicodeDecodeError, OSError):
                    reasons.append(f"unclassifiable_python_source:{path.relative_to(root).as_posix()}")
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = _call_name(node.func)
                    shell_true = name.startswith("subprocess.") and any(
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                        for keyword in node.keywords
                    )
                    if name in DANGEROUS_CALLS or shell_true:
                        reasons.append(f"dynamic_or_host_process_primitive:{name}")
        package_json = root / "package.json"
        if package_json.is_file() and not package_json.is_symlink():
            try:
                package = json.loads(package_json.read_text(encoding="utf-8"))
                scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
                if isinstance(scripts, dict) and any(
                    key in scripts for key in ("preinstall", "install", "postinstall")
                ):
                    reasons.append("package_install_lifecycle_script")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                reasons.append("unparseable_package_manifest")
        return ("high" if reasons else "standard"), sorted(set(reasons))


class MicroVMHTTPBackend:
    """Client for a trusted Firecracker supervisor implementing the v1 API contract."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: int = 900,
        expected_runner_image_digest: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MicroVM base URL must be an absolute credential-free HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in local_hosts:
            raise ValueError("MicroVM HTTP is permitted only for loopback development endpoints")
        if (
            not isinstance(api_key, str)
            or len(api_key.encode("utf-8")) < 24
            or any(marker in api_key.lower() for marker in ("placeholder", "replace-with", "redacted"))
        ):
            raise ValueError("MicroVM API key must be a real secret of at least 24 UTF-8 bytes")
        if timeout_seconds < 30 or timeout_seconds > 3600:
            raise ValueError("MicroVM timeout must be between 30 and 3600 seconds")
        if expected_runner_image_digest is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_runner_image_digest
        ):
            raise ValueError("Expected MicroVM runner image must be a pinned SHA-256 digest")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.expected_runner_image_digest = expected_runner_image_digest
        self.transport = transport

    @classmethod
    def from_env(cls) -> MicroVMHTTPBackend | None:
        base_url = os.getenv("RUSA_MICROVM_BASE_URL", "").strip()
        if not base_url:
            return None
        api_key = os.getenv("RUSA_MICROVM_API_KEY", "")
        production = os.getenv("ENVIRONMENT", "development").strip().lower() == "production"
        if production and urlparse(base_url).scheme != "https":
            raise ValueError("Production MicroVM supervisor must use HTTPS")
        expected_digest = os.getenv("RUSA_MICROVM_RUNNER_IMAGE_DIGEST", "").strip() or None
        if production and expected_digest is None:
            raise ValueError(
                "Production MicroVM requires RUSA_MICROVM_RUNNER_IMAGE_DIGEST"
            )
        return cls(
            base_url,
            api_key,
            timeout_seconds=int(os.getenv("RUSA_MICROVM_TIMEOUT_SECONDS", "900")),
            expected_runner_image_digest=expected_digest,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def verify_ready(self) -> None:
        timeout = httpx.Timeout(10.0, connect=5.0)
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=self.transport
        ) as client:
            with client.stream(
                "GET", f"{self.base_url}/health/ready", headers=self._headers()
            ) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 64_000:
                        raise RuntimeError("MicroVM readiness response exceeds 64 KB")
                    body.extend(chunk)
            payload = json.loads(bytes(body))
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            raise RuntimeError("MicroVM supervisor is not ready")
        if self.expected_runner_image_digest is not None:
            if payload.get("runner_image_digest") != self.expected_runner_image_digest:
                raise RuntimeError("MicroVM readiness runner image digest does not match the pinned image")

    @staticmethod
    def _archive_workspace(workspace: Path) -> tuple[bytes, str]:
        buffer = io.BytesIO()
        total_bytes = 0
        file_count = 0
        root = workspace.resolve(strict=True)
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for current, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = [
                        name for name in dirs
                        if name not in SKIP_DIRS
                        and name.lower() not in {
                            ".secrets", "secrets", "credentials", ".ssh", ".aws",
                            ".azure", ".terraform", ".rusa-ai",
                        }
                        and not (Path(current) / name).is_symlink()
                    ]
                    dirs.sort()
                    for name in sorted(files):
                        path = Path(current) / name
                        lowered = name.lower()
                        if (
                            path.is_symlink()
                            or lowered in SENSITIVE_NAMES
                            or (lowered.startswith(".env") and lowered != ".env.example")
                            or "credential" in lowered
                            or "secret" in lowered
                            or path.suffix.lower() in SENSITIVE_SUFFIXES
                            or lowered.endswith((".tfstate", ".tfstate.backup"))
                        ):
                            continue
                        resolved = path.resolve(strict=True)
                        if root not in resolved.parents:
                            continue
                        size = resolved.stat().st_size
                        total_bytes += size
                        file_count += 1
                        if total_bytes > MAX_SNAPSHOT_BYTES or file_count > MAX_FILES:
                            raise ValueError("MicroVM source snapshot exceeds configured file or byte limits")
                        relative = resolved.relative_to(root).as_posix()
                        info = tarfile.TarInfo(relative)
                        info.size = size
                        info.mode = 0o444
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        with resolved.open("rb") as source:
                            archive.addfile(info, source)
        data = buffer.getvalue()
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Compressed MicroVM source snapshot exceeds 100 MB")
        return data, hashlib.sha256(data).hexdigest()

    def run(self, workspace: Path, profile: str) -> ToolResult:
        if profile not in MICROVM_PROFILES:
            return ToolResult(False, f"MicroVM profile is not allowlisted: {profile}")
        try:
            snapshot, digest = self._archive_workspace(Path(workspace).resolve(strict=True))
            timeout = httpx.Timeout(self.timeout_seconds, connect=10.0)
            form = {
                "profile": profile,
                "timeout_seconds": str(self.timeout_seconds),
                "cpu_count": "2",
                "memory_mib": "2048",
                "snapshot_sha256": digest,
            }
            files = {"snapshot": ("workspace.tar.gz", snapshot, "application/gzip")}
            with httpx.Client(
                timeout=timeout, follow_redirects=False, trust_env=False, transport=self.transport
            ) as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/v1/executions",
                    headers=self._headers(),
                    data=form,
                    files=files,
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError("MicroVM response exceeds 1 MB")
                        body.extend(chunk)
            payload = json.loads(bytes(body))
            if not isinstance(payload, dict):
                raise ValueError("MicroVM response must be a JSON object")
            run_id = payload.get("run_id")
            exit_code = payload.get("exit_code")
            returned_digest = payload.get("snapshot_sha256")
            image_digest = payload.get("runner_image_digest")
            stdout = payload.get("stdout", "")
            stderr = payload.get("stderr", "")
            if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
                raise ValueError("MicroVM response has an invalid run_id")
            if type(exit_code) is not int:
                raise ValueError("MicroVM response has an invalid exit_code")
            if returned_digest != digest:
                raise ValueError("MicroVM snapshot digest mismatch")
            if not isinstance(image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
                raise ValueError("MicroVM response has no immutable runner image digest")
            if (
                self.expected_runner_image_digest is not None
                and image_digest != self.expected_runner_image_digest
            ):
                raise ValueError("MicroVM runner image digest does not match the pinned image")
            if not isinstance(stdout, str) or not isinstance(stderr, str):
                raise ValueError("MicroVM stdout/stderr must be text")
            output = (stdout + "\n" + stderr)[-20_000:]
            evidence = {
                "backend": "microvm",
                "profile": profile,
                "run_id": run_id,
                "exit_code": exit_code,
                "snapshot_sha256": digest,
                "runner_image_digest": image_digest,
            }
            return ToolResult(exit_code == 0, output, evidence)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return ToolResult(
                False,
                f"MicroVM execution failed closed: {type(exc).__name__}",
                {"backend": "microvm", "blocked": True, "error_type": type(exc).__name__},
            )


class HybridSandbox:
    """Route standard workloads to Docker and high-risk dynamic tests to MicroVM."""

    def __init__(
        self,
        docker: DockerSandbox,
        microvm: MicroVMHTTPBackend | None,
        classifier: SandboxRiskClassifier | None = None,
    ) -> None:
        self.docker = docker
        self.microvm = microvm
        self.classifier = classifier or SandboxRiskClassifier()

    def verify_ready(self, require_rootless: bool = False, require_image: bool = False) -> None:
        self.docker.verify_ready(
            require_rootless=require_rootless,
            require_image=require_image,
        )
        production = os.getenv("ENVIRONMENT", "development").strip().lower() == "production"
        raw_required = os.getenv("RUSA_MICROVM_REQUIRED")
        required = production if raw_required is None else raw_required.strip().lower() in {
            "1", "true", "yes", "on"
        }
        if raw_required is not None and raw_required.strip().lower() not in {
            "1", "true", "yes", "on", "0", "false", "no", "off"
        }:
            raise RuntimeError("RUSA_MICROVM_REQUIRED must be a boolean value")
        if production and raw_required is not None and raw_required.strip().lower() in {
            "0", "false", "no", "off"
        }:
            raise RuntimeError("RUSA_MICROVM_REQUIRED cannot disable MicroVM in production")
        if required and self.microvm is None:
            raise RuntimeError("Hybrid sandbox requires RUSA_MICROVM_BASE_URL and RUSA_MICROVM_API_KEY")
        if self.microvm is not None:
            self.microvm.verify_ready()

    def run(self, workspace: Path, profile: str) -> ToolResult:
        started = time.perf_counter()
        risk, reasons = self.classifier.classify(workspace, profile)
        if risk == "high":
            backend = "microvm"
            if self.microvm is None:
                result = ToolResult(
                    False,
                    "High-risk dynamic execution requires the configured MicroVM backend; Docker fallback is prohibited.",
                    {"backend": backend, "risk": risk, "blocked": True, "reasons": reasons},
                )
            else:
                result = self.microvm.run(workspace, profile)
                evidence = dict(result.evidence or {})
                evidence.update({"risk": risk, "risk_reasons": reasons})
                result = ToolResult(result.ok, result.output, evidence)
        else:
            backend = "docker"
            result = self.docker.run(workspace, profile)
            evidence = dict(result.evidence or {})
            evidence.update({"backend": backend, "risk": risk})
            result = ToolResult(result.ok, result.output, evidence)
        outcome = "success" if result.ok else (
            "blocked" if (result.evidence or {}).get("blocked") else "failure"
        )
        REFINERY_SANDBOX_RUNS.labels(backend, outcome).inc()
        REFINERY_SANDBOX_DURATION.labels(backend).observe(time.perf_counter() - started)
        return result
