"""Cryptographic provenance for sandbox-generated QA evidence.

The attestation proves that a trusted worker-host sandbox wrapper recorded a
specific validation profile, task, tenant, role, image, and immutable snapshot.
It does not replace a separately deployed Evaluation Service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4


class EvidenceAttestor:
    """Create and verify domain-separated HMAC attestations for sandbox checks."""

    VERSION = 1
    ISSUER = "rusa-docker-sandbox"
    _KEY_CONTEXT = b"rusa-ai:qa-sandbox-attestation:v1"
    _DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
    _FIELDS = frozenset(
        {
            "attestation_version",
            "issuer",
            "run_id",
            "profile",
            "exit_code",
            "image",
            "snapshot_sha256",
            "task_id",
            "tenant_id",
            "worker_role",
            "issued_at",
            "signature",
        }
    )

    def __init__(self, master_key: str | bytes) -> None:
        key = master_key.encode("utf-8") if isinstance(master_key, str) else master_key
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("QA attestation master key must contain at least 32 bytes")
        self._key = hmac.new(key, self._KEY_CONTEXT, hashlib.sha256).digest()

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def attest(
        self,
        *,
        profile: str,
        exit_code: int,
        image: str,
        snapshot_sha256: str,
        task_id: str,
        tenant_id: str,
        worker_role: str,
        issued_at: datetime | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(profile, str) or not profile:
            raise ValueError("validation profile is required")
        if type(exit_code) is not int:
            raise ValueError("validation exit code must be an integer")
        if not isinstance(image, str) or not image:
            raise ValueError("sandbox image reference is required")
        if not self._DIGEST_PATTERN.fullmatch(snapshot_sha256):
            raise ValueError("snapshot SHA-256 digest is invalid")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task identity is required for QA attestation")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant identity is required for QA attestation")
        if not isinstance(worker_role, str) or not worker_role:
            raise ValueError("worker role is required for QA attestation")

        timestamp = issued_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("QA attestation timestamp must be timezone-aware")
        payload: dict[str, Any] = {
            "attestation_version": self.VERSION,
            "issuer": self.ISSUER,
            "run_id": str(run_id or uuid4()),
            "profile": profile,
            "exit_code": exit_code,
            "image": image,
            "snapshot_sha256": snapshot_sha256,
            "task_id": task_id,
            "tenant_id": tenant_id,
            "worker_role": worker_role,
            "issued_at": timestamp.astimezone(timezone.utc).isoformat(),
        }
        payload["signature"] = hmac.new(
            self._key, self._canonical(payload), hashlib.sha256
        ).hexdigest()
        return payload

    def verify(
        self,
        evidence: Any,
        *,
        expected_profile: str,
        expected_worker_role: str,
        expected_task_id: str,
        expected_tenant_id: str,
        expected_image: str | None = None,
        max_age_seconds: int = 3600,
        now: datetime | None = None,
    ) -> bool:
        if not isinstance(evidence, dict) or set(evidence) != self._FIELDS:
            return False
        if evidence.get("attestation_version") != self.VERSION:
            return False
        if evidence.get("issuer") != self.ISSUER:
            return False
        if evidence.get("profile") != expected_profile:
            return False
        if evidence.get("worker_role") != expected_worker_role:
            return False
        if evidence.get("task_id") != expected_task_id:
            return False
        if evidence.get("tenant_id") != expected_tenant_id:
            return False
        if expected_image is not None and evidence.get("image") != expected_image:
            return False
        if type(evidence.get("exit_code")) is not int:
            return False
        if not isinstance(evidence.get("image"), str) or not evidence["image"]:
            return False
        if not isinstance(evidence.get("snapshot_sha256"), str) or not self._DIGEST_PATTERN.fullmatch(
            evidence["snapshot_sha256"]
        ):
            return False
        if not isinstance(evidence.get("signature"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", evidence["signature"]
        ):
            return False
        try:
            UUID(str(evidence["run_id"]))
            issued_at = datetime.fromisoformat(str(evidence["issued_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        if issued_at.tzinfo is None or issued_at.utcoffset() is None:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None or max_age_seconds < 0:
            return False
        age = current.astimezone(timezone.utc) - issued_at.astimezone(timezone.utc)
        if age > timedelta(seconds=max_age_seconds) or age < timedelta(seconds=-30):
            return False

        unsigned = {key: value for key, value in evidence.items() if key != "signature"}
        expected = hmac.new(
            self._key, self._canonical(unsigned), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, evidence["signature"])
