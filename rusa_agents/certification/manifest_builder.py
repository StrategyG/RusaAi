"""Build an evidence-bound production release manifest from verified OCI artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rusa_agents.certification.artifact_verification import verify_cosign_image_signature
from rusa_agents.certification.contracts import CertificationContractError, validate_release_manifest
from rusa_agents.certification.provenance_verification import verify_github_slsa_provenance

REQUIRED_ARTIFACTS = ("api", "worker", "sandbox", "web")
DEFAULT_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SLSA_V1 = "https://slsa.dev/provenance/v1"


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON evidence {path} must contain an object at its root")
    return value


def _sha256_file(path: str | Path) -> str:
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read evidence file {path}: {exc}") from exc
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _trivy_db_updated_at(path: str | Path) -> str:
    metadata = _read_json(path)
    updated_at = metadata.get("UpdatedAt")
    if not isinstance(updated_at, str) or not updated_at:
        raise ValueError("Trivy DB metadata must contain UpdatedAt")
    return _utc(updated_at)


def _vulnerability_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    results = report.get("Results")
    if not isinstance(results, list):
        raise ValueError("Trivy JSON report must contain a Results array")
    for result in results:
        if not isinstance(result, dict):
            continue
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise ValueError("Trivy Results.Vulnerabilities must be an array")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                continue
            severity = str(vulnerability.get("Severity", "")).lower()
            if severity in counts:
                counts[severity] += 1
    return counts


def build_release_manifest(
    *,
    repository: str,
    commit_sha: str,
    release_id: str,
    source_ref: str,
    workflow_run_id: int,
    workflow_path: str,
    signer_workflow: str,
    certificate_identity: str,
    oidc_issuer: str,
    trivy_db_metadata: str | Path,
    sbom_base_uri: str,
    artifacts: dict[str, dict[str, str]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify all image signatures/provenance and build a schema-validated manifest.

    The caller supplies exact image digests and scan/SBOM files generated from
    those digests. Cosign and GitHub SLSA provenance are verified here before
    any artifact is marked verified. Unapproved Critical or High findings are
    deliberately BLOCKED by the release-manifest contract.
    """
    if set(artifacts) != set(REQUIRED_ARTIFACTS):
        missing = sorted(set(REQUIRED_ARTIFACTS) - set(artifacts))
        extra = sorted(set(artifacts) - set(REQUIRED_ARTIFACTS))
        raise ValueError(f"artifact set mismatch; missing={missing}, extra={extra}")
    if len(commit_sha) != 40 or any(ch not in "0123456789abcdef" for ch in commit_sha):
        raise ValueError("commit_sha must be a lowercase 40-character Git SHA")
    if workflow_run_id < 1:
        raise ValueError("workflow_run_id must be positive")
    if not sbom_base_uri.startswith("https://"):
        raise ValueError("sbom_base_uri must be an HTTPS release-asset base URL")

    created_at = _now_iso(now)
    db_updated_at = _trivy_db_updated_at(trivy_db_metadata)
    identity = certificate_identity
    image_artifacts: list[dict[str, Any]] = []

    for name in REQUIRED_ARTIFACTS:
        item = artifacts[name]
        image = item["image"]
        digest = item["digest"]
        scan_report = _read_json(item["scan_json"])
        counts = _vulnerability_counts(scan_report)
        scanned_at = _now_iso(now)

        signature_result = verify_cosign_image_signature(
            image=image,
            digest=digest,
            certificate_identity=identity,
            oidc_issuer=oidc_issuer,
        )
        provenance_result = verify_github_slsa_provenance(
            image=image,
            digest=digest,
            repository=repository,
            signer_workflow=signer_workflow,
            source_digest=commit_sha,
            certificate_identity=identity,
            oidc_issuer=oidc_issuer,
        )
        sbom_digest = _sha256_file(item["sbom"])

        disposition = "BLOCKED" if counts["critical"] or counts["high"] else "PASS"
        image_artifacts.append(
            {
                "name": name,
                "image": image,
                "digest": digest,
                "signature": {
                    "verified": signature_result["verified"] is True,
                    "signer_identity": signature_result["signer_identity"],
                    "oidc_issuer": signature_result["oidc_issuer"],
                    "bundle_ref": f"oci://{image}@{digest}",
                },
                "provenance": {
                    "verified": provenance_result["verified"] is True,
                    "predicate_type": provenance_result["predicate_type"],
                    "source_repository": provenance_result["repository"],
                    "source_commit": provenance_result["source_digest"],
                    "workflow_identity": identity,
                    "attestation_ref": f"gh-attestation://{image}@{digest}",
                },
                "sbom": {
                    "format": "CycloneDX-JSON",
                    "uri": f"{sbom_base_uri.rstrip('/')}/sbom-{name}.cdx.json",
                    "digest": sbom_digest,
                    "subject_digest": digest,
                    "generated_by": "Trivy",
                },
                "vulnerability_scan": {
                    "scanner": "Trivy",
                    "scanned_at": scanned_at,
                    "database_updated_at": db_updated_at,
                    "subject_digest": digest,
                    "counts": counts,
                    "disposition": disposition,
                    "exceptions": [],
                },
            }
        )

    manifest = {
        "schema_version": "1.1",
        "release_id": release_id,
        "created_at": created_at,
        "source": {
            "repository": repository,
            "commit_sha": commit_sha,
            "ref": source_ref,
            "workflow": workflow_path,
            "workflow_run_id": workflow_run_id,
            "builder_identity": f"github-actions:{repository}",
        },
        "artifacts": image_artifacts,
    }
    validate_release_manifest(
        manifest,
        expected_repository=repository,
        expected_commit_sha=commit_sha,
        now=now,
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rusa_agents.certification.manifest_builder",
        description="Verify release image evidence and build release-manifest.json.",
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-path", default=".github/workflows/release.yml")
    parser.add_argument("--signer-workflow", default=".github/workflows/release.yml")
    parser.add_argument("--certificate-identity", required=True)
    parser.add_argument("--oidc-issuer", default=DEFAULT_OIDC_ISSUER)
    parser.add_argument("--trivy-db-metadata", required=True)
    parser.add_argument("--sbom-base-uri", required=True)
    parser.add_argument("--output", default="release-manifest.json")
    parser.add_argument(
        "--artifact",
        action="append",
        nargs=5,
        metavar=("NAME", "IMAGE", "DIGEST", "SCAN_JSON", "SBOM_JSON"),
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        artifacts: dict[str, dict[str, str]] = {}
        for name, image, digest, scan_json, sbom_json in args.artifact:
            if name in artifacts:
                raise ValueError(f"duplicate artifact name: {name}")
            artifacts[name] = {
                "image": image,
                "digest": digest,
                "scan_json": scan_json,
                "sbom": sbom_json,
            }
        manifest = build_release_manifest(
            repository=args.repository,
            commit_sha=args.commit_sha,
            release_id=args.release_id,
            source_ref=args.source_ref,
            workflow_run_id=args.workflow_run_id,
            workflow_path=args.workflow_path,
            signer_workflow=args.signer_workflow,
            certificate_identity=args.certificate_identity,
            oidc_issuer=args.oidc_issuer,
            trivy_db_metadata=args.trivy_db_metadata,
            sbom_base_uri=args.sbom_base_uri,
            artifacts=artifacts,
        )
        output = Path(args.output)
        output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        digest = _sha256_file(output)
        print(json.dumps({"status": "PASS", "manifest": str(output), "digest": digest}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
