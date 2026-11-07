"""Independent read-only QA service that signs test results for immutable Git trees."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .qa_attestation import (
    REQUIRED_QA_PROFILES,
    SERVICE_ID,
    VERDICT_SCHEMA,
    extract_snapshot_archive,
    load_private_key,
    sign_verdict,
)
from .tool_gateway import DockerSandbox

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TREE_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]+@sha256:[0-9a-f]{64}$")


class QAService:
    def __init__(
        self,
        *,
        token: str,
        private_key: Ed25519PrivateKey,
        key_id: str,
        sandbox: DockerSandbox,
        workspace_root: str | Path,
        max_upload_bytes: int = 100_000_000,
        max_files: int = 20_000,
        max_total_bytes: int = 100_000_000,
        max_file_bytes: int = 25_000_000,
        verdict_ttl_seconds: int = 900,
    ) -> None:
        if not isinstance(token, str) or len(token.encode("utf-8")) < 32:
            raise ValueError("QA service token must contain at least 32 bytes")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id):
            raise ValueError("QA signing key ID is invalid")
        if not 1 <= max_upload_bytes <= 250_000_000:
            raise ValueError("QA upload limit is outside the supported range")
        if not 1 <= max_files <= 50_000:
            raise ValueError("QA file-count limit is outside the supported range")
        if not 1 <= verdict_ttl_seconds <= 3600:
            raise ValueError("QA verdict TTL must be between 1 and 3600 seconds")
        self.token = token
        self.private_key = private_key
        self.key_id = key_id
        self.sandbox = sandbox
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.max_upload_bytes = max_upload_bytes
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_file_bytes = max_file_bytes
        self.verdict_ttl_seconds = verdict_ttl_seconds

    @classmethod
    def from_env(cls) -> QAService:
        token = os.getenv("RUSA_QA_SERVICE_TOKEN", "")
        private_key = load_private_key(os.getenv("RUSA_QA_PRIVATE_KEY_B64", ""))
        key_id = os.getenv("RUSA_QA_KEY_ID", "")
        image = os.getenv("RUSA_QA_SANDBOX_IMAGE", "")
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            if not _IMAGE_DIGEST.fullmatch(image):
                raise RuntimeError("RUSA_QA_SANDBOX_IMAGE must be pinned by sha256 digest")
        workspace_root = os.getenv("RUSA_QA_WORKSPACE_ROOT", "/tmp/rusa-qa")
        if not Path(workspace_root).is_absolute():
            raise RuntimeError("RUSA_QA_WORKSPACE_ROOT must be an absolute path")
        sandbox = DockerSandbox(
            image=image or None,
            timeout_seconds=int(os.getenv("RUSA_QA_CHECK_TIMEOUT_SECONDS", "600")),
        )
        sandbox.verify_ready(
            require_rootless=os.getenv("ENVIRONMENT", "development").lower() == "production",
            require_image=os.getenv("ENVIRONMENT", "development").lower() == "production",
        )
        return cls(
            token=token,
            private_key=private_key,
            key_id=key_id,
            sandbox=sandbox,
            workspace_root=workspace_root,
            max_upload_bytes=int(os.getenv("RUSA_QA_MAX_UPLOAD_BYTES", "100000000")),
            max_files=int(os.getenv("RUSA_QA_MAX_FILES", "20000")),
            max_total_bytes=int(os.getenv("RUSA_QA_MAX_TOTAL_BYTES", "100000000")),
            max_file_bytes=int(os.getenv("RUSA_QA_MAX_FILE_BYTES", "25000000")),
            verdict_ttl_seconds=int(os.getenv("RUSA_QA_VERDICT_TTL_SECONDS", "900")),
        )

    def validate(
        self,
        *,
        tenant_id: str,
        task_id: str,
        git_tree_sha: str,
        archive_bytes: bytes,
    ) -> dict[str, Any]:
        if not _IDENTIFIER.fullmatch(tenant_id) or not _IDENTIFIER.fullmatch(task_id):
            raise ValueError("QA tenant or task identifier is invalid")
        if not _TREE_SHA.fullmatch(git_tree_sha):
            raise ValueError("Git tree SHA is invalid")
        if len(archive_bytes) > self.max_upload_bytes:
            raise ValueError("Artifact archive exceeds the configured upload limit")

        with tempfile.TemporaryDirectory(
            prefix="rusa-qa-snapshot-",
            dir=self.workspace_root,
        ) as temporary:
            snapshot = Path(temporary) / "snapshot"
            artifact_digest = extract_snapshot_archive(
                archive_bytes,
                snapshot,
                max_files=self.max_files,
                max_total_bytes=self.max_total_bytes,
                max_file_bytes=self.max_file_bytes,
            )
            profile_results: dict[str, dict[str, Any]] = {}
            for profile in REQUIRED_QA_PROFILES:
                result = self.sandbox.run(snapshot, profile)
                evidence = result.evidence if isinstance(result.evidence, dict) else {}
                exit_code = evidence.get("exit_code")
                if (
                    type(exit_code) is not int
                    or evidence.get("profile") != profile
                    or evidence.get("image") != self.sandbox.image
                    or (not result.ok and exit_code == 0)
                ):
                    exit_code = 127
                profile_results[profile] = {
                    "exit_code": exit_code,
                    "output_sha256": hashlib.sha256(
                        result.output.encode("utf-8", "replace")
                    ).hexdigest(),
                }

            issued_at = datetime.now(timezone.utc)
            payload = {
                "schema_version": VERDICT_SCHEMA,
                "service_id": SERVICE_ID,
                "key_id": self.key_id,
                "tenant_id": tenant_id,
                "task_id": task_id,
                "git_tree_sha": git_tree_sha,
                "artifact_digest": artifact_digest,
                "sandbox_image": self.sandbox.image,
                "profiles": profile_results,
                "verdict": (
                    "PASS"
                    if all(item["exit_code"] == 0 for item in profile_results.values())
                    else "FAIL"
                ),
                "issued_at": issued_at.isoformat(timespec="seconds"),
                "expires_at": (
                    issued_at + timedelta(seconds=self.verdict_ttl_seconds)
                ).isoformat(timespec="seconds"),
            }
            return sign_verdict(payload, self.private_key)


def create_app(service: QAService | None = None) -> FastAPI:
    app = FastAPI(title="RUSA-AI Independent QA", docs_url=None, redoc_url=None)
    app.state.qa_service = service

    @app.on_event("startup")
    async def startup() -> None:
        if app.state.qa_service is None:
            app.state.qa_service = QAService.from_env()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        if app.state.qa_service is None:
            raise HTTPException(status_code=503, detail="QA service is not initialized")
        return {"status": "ready"}

    @app.post("/v1/validate")
    async def validate(request: Request) -> JSONResponse:
        active: QAService | None = app.state.qa_service
        if active is None:
            raise HTTPException(status_code=503, detail="QA service is not initialized")
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(authorization, f"Bearer {active.token}"):
            raise HTTPException(status_code=401, detail="unauthorized")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-tar":
            raise HTTPException(status_code=415, detail="unsupported artifact content type")

        tenant_id = request.headers.get("x-rusa-tenant-id", "")
        task_id = request.headers.get("x-rusa-task-id", "")
        git_tree_sha = request.headers.get("x-rusa-git-tree-sha", "")
        if (
            not _IDENTIFIER.fullmatch(tenant_id)
            or not _IDENTIFIER.fullmatch(task_id)
            or not _TREE_SHA.fullmatch(git_tree_sha)
        ):
            raise HTTPException(status_code=400, detail="invalid task binding headers")

        content_length = request.headers.get("content-length")
        if content_length is None or not content_length.isdigit():
            raise HTTPException(status_code=411, detail="content length is required")
        if int(content_length) > active.max_upload_bytes:
            raise HTTPException(status_code=413, detail="artifact upload exceeds size limit")

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > active.max_upload_bytes:
                raise HTTPException(status_code=413, detail="artifact upload exceeds size limit")
            chunks.append(chunk)
        if total != int(content_length):
            raise HTTPException(status_code=400, detail="artifact upload length mismatch")

        try:
            envelope = await asyncio.to_thread(
                active.validate,
                tenant_id=tenant_id,
                task_id=task_id,
                git_tree_sha=git_tree_sha,
                archive_bytes=b"".join(chunks),
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="QA execution is unavailable") from exc
        return JSONResponse(envelope)

    return app


app = create_app()
