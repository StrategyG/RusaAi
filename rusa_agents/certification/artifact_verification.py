"""Fail-closed Cosign verification for immutable OCI image references."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class ArtifactVerificationError(RuntimeError):
    """Raised when trusted artifact verification cannot be completed."""


def verify_cosign_image_signature(
    *,
    image: str,
    digest: str,
    certificate_identity: str,
    oidc_issuer: str,
    timeout_seconds: int = 30,
    cosign_binary: str = "cosign",
) -> dict[str, Any]:
    """Verify a Cosign image signature and bind its verified subject to digest.

    Cosign enforces the expected certificate identity and OIDC issuer. The
    returned verification result is only produced when Cosign succeeds and its
    JSON output names the exact requested manifest digest.
    """
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("digest must be a lowercase sha256 digest")
    if not image or "@" in image or any(char.isspace() for char in image):
        raise ValueError("image must be a non-empty repository reference without a digest")
    if not certificate_identity.strip():
        raise ValueError("certificate_identity must not be empty")
    if not oidc_issuer.startswith("https://"):
        raise ValueError("oidc_issuer must be an HTTPS URL")
    if not 1 <= timeout_seconds <= 120:
        raise ValueError("timeout_seconds must be between 1 and 120")
    executable = shutil.which(cosign_binary)
    if executable is None:
        raise ArtifactVerificationError("Cosign executable is not installed")

    command = [
        executable,
        "verify",
        "--certificate-identity",
        certificate_identity,
        "--certificate-oidc-issuer",
        oidc_issuer,
        f"{image}@{digest}",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArtifactVerificationError("Cosign verification timed out") from exc
    except OSError as exc:
        raise ArtifactVerificationError("Cosign could not be executed") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500] or "no diagnostic output"
        raise ArtifactVerificationError(f"Cosign verification failed: {detail}")

    try:
        verification_records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ArtifactVerificationError("Cosign returned invalid JSON") from exc
    if not isinstance(verification_records, list) or not verification_records:
        raise ArtifactVerificationError("Cosign returned no verified signature records")

    digest_matches = any(
        isinstance(record, dict)
        and isinstance(record.get("critical"), dict)
        and isinstance(record["critical"].get("image"), dict)
        and record["critical"]["image"].get("docker-manifest-digest") == digest
        for record in verification_records
    )
    if not digest_matches:
        raise ArtifactVerificationError(
            "Cosign verification output does not bind to the requested image digest"
        )

    return {
        "verified": True,
        "signer_identity": certificate_identity,
        "oidc_issuer": oidc_issuer,
        "subject_digest": digest,
        "verification_tool": "cosign",
    }
