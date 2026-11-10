"""GitHub SLSA provenance verification for immutable OCI image digests."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from rusa_agents.certification.artifact_verification import ArtifactVerificationError

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SOURCE_DIGEST_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_SLSA_V1 = "https://slsa.dev/provenance/v1"


def verify_github_slsa_provenance(
    *,
    image: str,
    digest: str,
    repository: str,
    signer_workflow: str,
    source_digest: str,
    certificate_identity: str,
    oidc_issuer: str = "https://token.actions.githubusercontent.com",
    timeout_seconds: int = 60,
    gh_binary: str = "gh",
) -> dict[str, Any]:
    """Verify a GitHub SLSA v1 attestation and bind it to the requested digest.

    GitHub CLI verifies the attestation cryptographically and enforces the
    repository, signer workflow, certificate identity, OIDC issuer, and
    predicate type. The parsed statement is additionally checked for the
    exact SHA-256 image subject before evidence is returned.
    """
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("digest must be a lowercase sha256 digest")
    if not image or "@" in image or any(char.isspace() for char in image):
        raise ValueError("image must be a non-empty repository reference without a digest")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must use owner/name form")
    if not signer_workflow.strip() or not certificate_identity.strip():
        raise ValueError("signer_workflow and certificate_identity must not be empty")
    if not _SOURCE_DIGEST_RE.fullmatch(source_digest):
        raise ValueError("source_digest must be a 40- or 64-character lowercase Git commit SHA")
    if not oidc_issuer.startswith("https://"):
        raise ValueError("oidc_issuer must be an HTTPS URL")
    if not 1 <= timeout_seconds <= 180:
        raise ValueError("timeout_seconds must be between 1 and 180")

    executable = shutil.which(gh_binary)
    if executable is None:
        raise ArtifactVerificationError("GitHub CLI executable is not installed")

    command = [
        executable,
        "attestation",
        "verify",
        f"oci://{image}@{digest}",
        "--repo",
        repository,
        "--signer-workflow",
        signer_workflow,
        "--source-digest",
        source_digest,
        "--cert-identity",
        certificate_identity,
        "--cert-oidc-issuer",
        oidc_issuer,
        "--predicate-type",
        _SLSA_V1,
        "--format",
        "json",
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
        raise ArtifactVerificationError("GitHub attestation verification timed out") from exc
    except OSError as exc:
        raise ArtifactVerificationError("GitHub CLI could not be executed") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500] or "no diagnostic output"
        raise ArtifactVerificationError(f"SLSA provenance verification failed: {detail}")

    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ArtifactVerificationError("GitHub CLI returned invalid attestation JSON") from exc
    if not isinstance(records, list) or not records:
        raise ArtifactVerificationError("GitHub CLI returned no verified SLSA attestations")

    def subject_matches(record: object) -> bool:
        if not isinstance(record, dict):
            return False
        result = record.get("verificationResult")
        if not isinstance(result, dict):
            return False
        statement = result.get("statement")
        if not isinstance(statement, dict) or statement.get("predicateType") != _SLSA_V1:
            return False
        subjects = statement.get("subject")
        if not isinstance(subjects, list):
            return False
        return any(
            isinstance(subject, dict)
            and isinstance(subject.get("digest"), dict)
            and subject["digest"].get("sha256") == digest.removeprefix("sha256:")
            for subject in subjects
        )

    if not any(subject_matches(record) for record in records):
        raise ArtifactVerificationError(
            "verified SLSA attestation does not contain the requested image digest as subject"
        )

    return {
        "verified": True,
        "repository": repository,
        "signer_workflow": signer_workflow,
        "source_digest": source_digest,
        "certificate_identity": certificate_identity,
        "oidc_issuer": oidc_issuer,
        "predicate_type": _SLSA_V1,
        "subject_digest": digest,
        "verification_tool": "gh attestation verify",
    }
