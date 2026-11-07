"""Worker-side client for the independent, signed QA service."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .contracts import CodingTask
from .qa_attestation import (
    REQUIRED_QA_PROFILES,
    AttestationError,
    load_public_key,
    verify_verdict,
)


class QAServiceUnavailable(RuntimeError):
    """Raised when the independent QA service cannot produce a verdict."""


class QAAttestationError(RuntimeError):
    """Raised when QA returns an invalid, stale, or task-mismatched verdict."""


class IndependentQAClient:
    def __init__(
        self,
        *,
        publisher: Any,
        workspace: str | Path,
        service_url: str,
        service_token: str,
        public_key_b64: str,
        key_id: str,
        sandbox_image: str,
        timeout_seconds: float = 2700,
    ) -> None:
        parsed = urlparse(service_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("QA service URL must be an absolute credential-free HTTP(S) URL")
        if len(service_token.encode("utf-8")) < 32:
            raise ValueError("QA service token must contain at least 32 bytes")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id):
            raise ValueError("QA signing key ID is invalid")
        self.publisher = publisher
        self.workspace = Path(workspace)
        self.service_url = service_url.rstrip("/")
        self.service_token = service_token
        self.public_key = load_public_key(public_key_b64)
        self.key_id = key_id
        self.sandbox_image = sandbox_image
        self.timeout_seconds = timeout_seconds

    async def validate(self, task: CodingTask) -> dict[str, Any]:
        try:
            archive, tree_sha = await asyncio.to_thread(
                self.publisher.prepare_candidate_snapshot,
                self.workspace,
            )
        except Exception as exc:
            raise QAAttestationError(
                f"Could not create the immutable candidate artifact snapshot: {exc}"
            ) from exc
        tenant_id = str(task.metadata.get("tenant_id", "default"))
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "Content-Type": "application/x-tar",
            "X-Rusa-Tenant-ID": tenant_id,
            "X-Rusa-Task-ID": task.task_id,
            "X-Rusa-Git-Tree-SHA": tree_sha,
            "Content-Length": str(len(archive)),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self.service_url}/v1/validate",
                    content=archive,
                    headers=headers,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise QAServiceUnavailable("Independent QA service is unreachable or timed out") from exc
        except httpx.HTTPError as exc:
            raise QAServiceUnavailable("Independent QA request failed") from exc

        if response.status_code >= 500:
            raise QAServiceUnavailable(
                f"Independent QA service returned HTTP {response.status_code}"
            )
        if response.status_code != 200:
            raise QAAttestationError(
                f"Independent QA service rejected the snapshot (HTTP {response.status_code})"
            )
        try:
            envelope = response.json()
            self.verify_release_verdict(
                envelope,
                task,
                expected_tree_sha=tree_sha,
                require_pass=False,
            )
        except (ValueError, TypeError, AttestationError) as exc:
            raise QAAttestationError(f"Independent QA verdict is invalid: {exc}") from exc
        return envelope

    def verify_release_verdict(
        self,
        envelope: dict[str, Any] | None,
        task: CodingTask,
        *,
        expected_tree_sha: str | None = None,
        require_pass: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            raise QAAttestationError("Independent QA verdict is missing")
        payload = envelope["payload"]
        tenant_id = str(task.metadata.get("tenant_id", "default"))
        tree_sha = expected_tree_sha or payload.get("git_tree_sha")
        try:
            verified = verify_verdict(
                envelope,
                self.public_key,
                tenant_id=tenant_id,
                task_id=task.task_id,
                git_tree_sha=tree_sha,
                key_id=self.key_id,
                sandbox_image=self.sandbox_image,
            )
        except AttestationError as exc:
            raise QAAttestationError(str(exc)) from exc
        if set(verified["profiles"]) != set(REQUIRED_QA_PROFILES):
            raise QAAttestationError("Independent QA verdict omitted a mandatory profile")
        if require_pass and verified["verdict"] != "PASS":
            raise QAAttestationError("Independent QA did not issue a PASS verdict")
        return verified
