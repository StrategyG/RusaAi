"""Validation and deterministic evaluation for production certification evidence.

This module validates evidence structure and cross-document bindings. It does
not perform cryptographic signature verification, scan images, run penetration
tests, restore backups, or authenticate human reviewers; those operations must
be performed by their trusted CI/deployment systems and referenced here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "certification"
_SCHEMA_FILES = {
    "release_manifest": "release-manifest.schema.json",
    "production_certification": "production-certification.schema.json",
}
_REQUIRED_ARTIFACTS = frozenset({"api", "worker", "sandbox"})
_REQUIRED_SIGNOFFS = frozenset(
    {"platform", "security", "sre", "qa", "data_governance", "finops", "process_owner"}
)


class CertificationContractError(ValueError):
    """Raised when a release or certification document violates its contract."""


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    return current.astimezone(timezone.utc)


@lru_cache(maxsize=len(_SCHEMA_FILES))
def _validator(contract_name: str) -> jsonschema.Draft202012Validator:
    try:
        filename = _SCHEMA_FILES[contract_name]
    except KeyError as exc:
        raise ValueError(f"unknown certification contract: {contract_name}") from exc

    path = _SCHEMA_DIR / filename
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load certification schema: {filename}") from exc

    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise RuntimeError(f"invalid certification schema {filename}: {exc.message}") from exc

    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


def _schema_errors(contract_name: str, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["document root must be a JSON object"]
    errors = sorted(
        _validator(contract_name).iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"{error.message}"
        for error in errors[:50]
    ]


def validate_production_certification(payload: dict[str, Any]) -> None:
    """Validate the versioned certification evidence document schema."""
    errors = _schema_errors("production_certification", payload)
    if errors:
        raise CertificationContractError(
            "production certification contract validation failed: " + "; ".join(errors)
        )


def validate_release_manifest(
    payload: dict[str, Any],
    *,
    expected_repository: str | None = None,
    expected_commit_sha: str | None = None,
    now: datetime | None = None,
) -> None:
    """Validate release manifest schema and source/artifact security bindings.

    Signature and provenance `verified` values are assertions from a trusted
    verifier. This function checks their presence and consistency; it does not
    cryptographically verify the referenced bundles.
    """
    errors = _schema_errors("release_manifest", payload)
    if errors:
        raise CertificationContractError(
            "release manifest contract validation failed: " + "; ".join(errors)
        )

    manifest_signature = payload["manifest_signature"]
    if manifest_signature["verified"] is not True:
        errors.append("release manifest signature is not verified")

    source = payload["source"]
    repository = source["repository"]
    commit_sha = source["commit_sha"].lower()
    if expected_repository and repository != expected_repository:
        errors.append("source.repository does not match the expected repository")
    if expected_commit_sha and commit_sha != expected_commit_sha.lower():
        errors.append("source.commit_sha does not match the expected candidate commit")

    artifacts = payload["artifacts"]
    names = [artifact["name"] for artifact in artifacts]
    missing = sorted(_REQUIRED_ARTIFACTS - set(names))
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    if missing:
        errors.append(f"required artifacts are missing: {', '.join(missing)}")
    if duplicates:
        errors.append(f"artifact names must be unique: {', '.join(duplicates)}")

    current = _utc_now(now)
    for artifact in artifacts:
        label = artifact["name"]
        image_suffix = label.replace("_", "-")
        expected_image = f"ghcr.io/{repository.split('/', 1)[0].lower()}/rusa-ai-{image_suffix}"
        if artifact["image"] != expected_image:
            errors.append(f"{label}: image reference is outside the expected RUSA-AI registry path")

        signature = artifact["signature"]
        if signature["verified"] is not True:
            errors.append(f"{label}: image signature is not verified")

        provenance = artifact["provenance"]
        if provenance["verified"] is not True:
            errors.append(f"{label}: build provenance is not verified")
        if provenance["source_repository"] != repository:
            errors.append(f"{label}: provenance repository does not match release source")
        if provenance["source_commit"].lower() != commit_sha:
            errors.append(f"{label}: provenance commit does not match release source commit")

        sbom = artifact["sbom"]
        if sbom["subject_digest"] != artifact["digest"]:
            errors.append(f"{label}: SBOM subject digest does not match image digest")

        scan = artifact["vulnerability_scan"]
        if scan["subject_digest"] != artifact["digest"]:
            errors.append(f"{label}: vulnerability scan subject digest does not match image digest")
        scanned_at = datetime.fromisoformat(scan["scanned_at"].replace("Z", "+00:00"))
        database_updated_at = datetime.fromisoformat(
            scan["database_updated_at"].replace("Z", "+00:00")
        )
        if scanned_at.astimezone(timezone.utc) > current:
            errors.append(f"{label}: vulnerability scan timestamp is in the future")
        if database_updated_at.astimezone(timezone.utc) > scanned_at.astimezone(timezone.utc):
            errors.append(f"{label}: scanner database timestamp is later than the scan")

        counts = scan["counts"]
        if counts["critical"] > 0:
            errors.append(f"{label}: Critical vulnerabilities block release")
        if scan["disposition"] == "BLOCKED":
            errors.append(f"{label}: vulnerability policy disposition is BLOCKED")
        if counts["high"] == 0 and scan["disposition"] != "PASS":
            errors.append(f"{label}: non-PASS vulnerability disposition has no High findings")
        if counts["high"] == 0 and scan["exceptions"]:
            errors.append(f"{label}: vulnerability exceptions exist without High findings")
        if counts["high"] > 0:
            exceptions = scan["exceptions"]
            if scan["disposition"] != "EXCEPTION":
                errors.append(f"{label}: High vulnerabilities require an approved EXCEPTION disposition")
            if len(exceptions) < counts["high"]:
                errors.append(f"{label}: every High finding requires an individual exception")
            finding_ids = [exception["finding_id"] for exception in exceptions]
            if len(finding_ids) != len(set(finding_ids)):
                errors.append(f"{label}: vulnerability exception finding IDs must be unique")
            for exception in exceptions:
                approval_time = datetime.fromisoformat(
                    exception["approved_at"].replace("Z", "+00:00")
                )
                if approval_time.astimezone(timezone.utc) > current:
                    errors.append(
                        f"{label}: vulnerability exception {exception['finding_id']} approval is in the future"
                    )
                expiry = datetime.fromisoformat(exception["expires_at"].replace("Z", "+00:00"))
                if expiry.astimezone(timezone.utc) <= current:
                    errors.append(
                        f"{label}: vulnerability exception {exception['finding_id']} has expired"
                    )

    if errors:
        raise CertificationContractError("; ".join(errors))


def evaluate_production_certification(
    payload: dict[str, Any],
    *,
    expected_commit_sha: str,
    expected_release_manifest_digest: str,
    expected_repository: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return PASS/HOLD/FAIL for an exact candidate and release-manifest digest.

    PASS means all declared evidence checks and seven accountable sign-offs are
    present and marked PASS/approved. The caller must first verify artifact
    attestations and reviewer identities through trusted GitHub/OIDC controls.
    """
    current = _utc_now(now)
    schema_errors = _schema_errors("production_certification", payload)
    if schema_errors:
        return {
            "schema_version": "1.0",
            "status": "HOLD",
            "candidate_sha": None,
            "evaluated_at": current.isoformat(),
            "blockers": schema_errors,
        }

    failures: list[str] = []
    holds: list[str] = []
    candidate = payload["candidate"]
    candidate_sha = candidate["commit_sha"].lower()

    if candidate_sha != expected_commit_sha.lower():
        failures.append("certification candidate SHA does not match the expected commit")
    if expected_repository and candidate["repository"] != expected_repository:
        failures.append("certification repository does not match the expected repository")
    if candidate["release_manifest_ref"]["digest"] != expected_release_manifest_digest:
        failures.append("release manifest digest does not match the expected immutable digest")
    if payload["source"]["attestation_verified"] is not True:
        holds.append("certification evidence attestation has not been verified")

    for check_name, check in payload["checks"].items():
        if check["subject_commit_sha"].lower() != candidate_sha:
            failures.append(f"{check_name}: evidence targets a different source commit")
        if check["status"] == "FAIL":
            failures.append(f"{check_name}: mandatory certification check failed")
        elif check["status"] == "HOLD":
            holds.append(f"{check_name}: mandatory certification evidence is on HOLD")

    for owner in sorted(_REQUIRED_SIGNOFFS):
        signoff = payload["signoffs"][owner]
        if signoff["approved"] is not True:
            holds.append(f"{owner}: required human sign-off is not approved")
        approved_at = datetime.fromisoformat(signoff["approved_at"].replace("Z", "+00:00"))
        if approved_at.astimezone(timezone.utc) > current:
            holds.append(f"{owner}: sign-off timestamp is in the future")

    blockers = failures + holds
    status = "FAIL" if failures else "HOLD" if holds else "PASS"
    return {
        "schema_version": "1.0",
        "certification_id": payload["certification_id"],
        "status": status,
        "candidate_sha": candidate_sha,
        "release_manifest_digest": candidate["release_manifest_ref"]["digest"],
        "target_environment": candidate["target_environment"],
        "evaluated_at": current.isoformat(),
        "blockers": blockers,
    }
