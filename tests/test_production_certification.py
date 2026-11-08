from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rusa_agents.certification.contracts import (
    CertificationContractError,
    evaluate_production_certification,
    validate_production_certification,
    validate_release_manifest,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
COMMIT = "a" * 40
MANIFEST_DIGEST = "sha256:" + "f" * 64
ARTIFACT_DIGESTS = {
    "api": "sha256:" + "1" * 64,
    "worker": "sha256:" + "2" * 64,
    "sandbox": "sha256:" + "3" * 64,
}


def _signature() -> dict[str, object]:
    return {
        "verified": True,
        "signer_identity": "https://github.com/StrategyG/RusaAi/.github/workflows/release.yml@refs/tags/v1.0.0",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "bundle_ref": "oci://ghcr.io/strategyg/rusa-ai-attestations@sha256:" + "4" * 64,
    }


def release_manifest() -> dict[str, object]:
    artifacts = []
    for name, digest in ARTIFACT_DIGESTS.items():
        artifacts.append(
            {
                "name": name,
                "image": f"ghcr.io/strategyg/rusa-ai-{name}",
                "digest": digest,
                "signature": _signature(),
                "provenance": {
                    "verified": True,
                    "predicate_type": "https://slsa.dev/provenance/v1",
                    "source_repository": "StrategyG/RusaAi",
                    "source_commit": COMMIT,
                    "workflow_identity": "StrategyG/RusaAi/.github/workflows/release.yml",
                    "attestation_ref": "oci://ghcr.io/strategyg/rusa-ai-attestations/provenance",
                },
                "sbom": {
                    "format": "SPDX-JSON",
                    "uri": f"oci://ghcr.io/strategyg/rusa-ai-sbom/{name}",
                    "digest": "sha256:" + "5" * 64,
                    "subject_digest": digest,
                    "generated_by": "BuildKit",
                },
                "vulnerability_scan": {
                    "scanner": "Trivy",
                    "scanned_at": "2026-11-08T10:00:00Z",
                    "database_updated_at": "2026-11-08T09:00:00Z",
                    "subject_digest": digest,
                    "counts": {"critical": 0, "high": 0, "medium": 2, "low": 4},
                    "disposition": "PASS",
                    "exceptions": [],
                },
            }
        )
    return {
        "schema_version": "1.0",
        "release_id": "v1.0.0",
        "created_at": "2026-11-08T10:30:00Z",
        "source": {
            "repository": "StrategyG/RusaAi",
            "commit_sha": COMMIT,
            "ref": "refs/tags/v1.0.0",
            "workflow": ".github/workflows/release.yml",
            "workflow_run_id": 424242,
            "builder_identity": "github-actions:StrategyG/RusaAi",
        },
        "artifacts": artifacts,
    }


def certification_record() -> dict[str, object]:
    check_names = (
        "release_integrity",
        "supply_chain",
        "vulnerability",
        "tenant_isolation",
        "penetration_test",
        "backup_restore",
        "disaster_recovery",
        "rollback",
        "observability",
        "slo",
        "staging_soak",
        "independent_qa",
        "erg06",
    )
    checks = {
        name: {
            "status": "PASS",
            "evidence_ref": f"artifact://certification/{name}.json",
            "evidence_digest": "sha256:" + "6" * 64,
            "subject_commit_sha": COMMIT,
            "observed_at": "2026-11-08T11:00:00Z",
            "summary": f"{name} verified for the candidate",
        }
        for name in check_names
    }
    owners = (
        "platform",
        "security",
        "sre",
        "qa",
        "data_governance",
        "finops",
        "process_owner",
    )
    signoffs = {
        owner: {
            "reviewer": f"{owner}-reviewer",
            "approved": True,
            "approved_at": "2026-11-08T11:30:00Z",
            "evidence_ref": f"approval://{owner}/v1.0.0",
        }
        for owner in owners
    }
    return {
        "schema_version": "1.0",
        "certification_id": "d9428888-122b-4a7a-9e9c-2c6d3c9f3f11",
        "generated_at": "2026-11-08T11:45:00Z",
        "candidate": {
            "repository": "StrategyG/RusaAi",
            "commit_sha": COMMIT,
            "evidence_environment": "staging",
            "target_environment": "production",
            "release_id": "v1.0.0",
            "release_manifest_ref": {
                "uri": "artifact://release/v1.0.0/release-manifest.json",
                "digest": MANIFEST_DIGEST,
                "signature": _signature(),
            },
        },
        "source": {
            "workflow": ".github/workflows/production-certification.yml",
            "run_id": 424243,
            "attestation_verified": True,
            "attestation_ref": "artifact://attestations/certification.json",
        },
        "checks": checks,
        "signoffs": signoffs,
    }


def test_release_manifest_validates_all_required_immutable_artifacts() -> None:
    validate_release_manifest(
        release_manifest(),
        expected_repository="StrategyG/RusaAi",
        expected_commit_sha=COMMIT,
        now=NOW,
    )


def test_certification_holds_when_detached_manifest_signature_is_not_verified() -> None:
    payload = certification_record()
    payload["candidate"]["release_manifest_ref"]["signature"]["verified"] = False
    result = evaluate_production_certification(
        payload,
        expected_commit_sha=COMMIT,
        expected_release_manifest_digest=MANIFEST_DIGEST,
        expected_repository="StrategyG/RusaAi",
        now=NOW,
    )
    assert result["status"] == "HOLD"
    assert any("detached signature" in blocker for blocker in result["blockers"])


def test_release_manifest_rejects_image_outside_expected_registry_path() -> None:
    payload = release_manifest()
    payload["artifacts"][0]["image"] = "ghcr.io/untrusted/rusa-ai-api"
    with pytest.raises(CertificationContractError, match="outside the expected RUSA-AI registry path"):
        validate_release_manifest(payload, now=NOW)


def test_release_manifest_rejects_provenance_from_another_commit() -> None:
    payload = release_manifest()
    payload["artifacts"][0]["provenance"]["source_commit"] = "b" * 40
    with pytest.raises(CertificationContractError, match="provenance commit"):
        validate_release_manifest(payload, now=NOW)


def test_release_manifest_rejects_unverified_image_signature() -> None:
    payload = release_manifest()
    payload["artifacts"][1]["signature"]["verified"] = False
    with pytest.raises(CertificationContractError, match="signature is not verified"):
        validate_release_manifest(payload, now=NOW)


def test_release_manifest_rejects_sbom_for_different_image_digest() -> None:
    payload = release_manifest()
    payload["artifacts"][0]["sbom"]["subject_digest"] = "sha256:" + "9" * 64
    with pytest.raises(CertificationContractError, match="SBOM subject digest"):
        validate_release_manifest(payload, now=NOW)


def test_release_manifest_rejects_scan_for_different_digest() -> None:
    payload = release_manifest()
    payload["artifacts"][0]["vulnerability_scan"]["subject_digest"] = "sha256:" + "9" * 64
    with pytest.raises(CertificationContractError, match="subject digest"):
        validate_release_manifest(payload, now=NOW)


def test_release_manifest_blocks_critical_and_unexcepted_high_findings() -> None:
    critical = release_manifest()
    critical["artifacts"][0]["vulnerability_scan"]["counts"]["critical"] = 1
    with pytest.raises(CertificationContractError, match="Critical vulnerabilities"):
        validate_release_manifest(critical, now=NOW)

    high = release_manifest()
    scan = high["artifacts"][0]["vulnerability_scan"]
    scan["counts"]["high"] = 1
    with pytest.raises(CertificationContractError, match="High vulnerabilities require"):
        validate_release_manifest(high, now=NOW)


def test_release_manifest_accepts_approved_unexpired_high_exception() -> None:
    payload = release_manifest()
    scan = payload["artifacts"][0]["vulnerability_scan"]
    scan["counts"]["high"] = 1
    scan["disposition"] = "EXCEPTION"
    scan["exceptions"] = [
        {
            "finding_id": "CVE-2026-12345",
            "severity": "HIGH",
            "owner": "security-owner",
            "rationale": "No vendor fix is available; exposure is constrained.",
            "expires_at": "2026-12-01T00:00:00Z",
            "approval_ref": "approval://security/CVE-2026-12345",
            "approved": True,
            "approved_by": "security-reviewer",
            "approved_at": "2026-11-08T10:00:00Z",
        }
    ]
    validate_release_manifest(payload, now=NOW)


def test_release_manifest_rejects_unapproved_exception() -> None:
    payload = release_manifest()
    scan = payload["artifacts"][0]["vulnerability_scan"]
    scan["counts"]["high"] = 1
    scan["disposition"] = "EXCEPTION"
    scan["exceptions"] = [
        {
            "finding_id": "CVE-2026-12345",
            "severity": "HIGH",
            "owner": "security-owner",
            "rationale": "Temporary exception pending approval.",
            "expires_at": "2026-12-01T00:00:00Z",
            "approval_ref": "approval://security/CVE-2026-12345",
            "approved": False,
            "approved_by": "security-reviewer",
            "approved_at": "2026-11-08T10:00:00Z",
        }
    ]
    with pytest.raises(CertificationContractError, match="approved.*True was expected"):
        validate_release_manifest(payload, now=NOW)


def test_release_manifest_rejects_expired_exception() -> None:
    payload = release_manifest()
    scan = payload["artifacts"][0]["vulnerability_scan"]
    scan["counts"]["high"] = 1
    scan["disposition"] = "EXCEPTION"
    scan["exceptions"] = [
        {
            "finding_id": "CVE-2026-12345",
            "severity": "HIGH",
            "owner": "security-owner",
            "rationale": "Temporary exception.",
            "expires_at": "2026-11-01T00:00:00Z",
            "approval_ref": "approval://security/CVE-2026-12345",
            "approved": True,
            "approved_by": "security-reviewer",
            "approved_at": "2026-11-08T10:00:00Z",
        }
    ]
    with pytest.raises(CertificationContractError, match="has expired"):
        validate_release_manifest(payload, now=NOW)


def test_certification_passes_only_for_exact_commit_and_manifest_with_signoffs() -> None:
    payload = certification_record()
    validate_production_certification(payload)
    result = evaluate_production_certification(
        payload,
        expected_commit_sha=COMMIT,
        expected_release_manifest_digest=MANIFEST_DIGEST,
        expected_repository="StrategyG/RusaAi",
        now=NOW,
    )
    assert result["status"] == "PASS"
    assert result["blockers"] == []


def test_certification_holds_when_human_signoff_is_missing() -> None:
    payload = certification_record()
    payload["signoffs"]["security"]["approved"] = False
    result = evaluate_production_certification(
        payload,
        expected_commit_sha=COMMIT,
        expected_release_manifest_digest=MANIFEST_DIGEST,
        expected_repository="StrategyG/RusaAi",
        now=NOW,
    )
    assert result["status"] == "HOLD"
    assert any("security" in blocker for blocker in result["blockers"])


def test_certification_fails_when_evidence_targets_another_commit() -> None:
    payload = certification_record()
    payload["checks"]["backup_restore"]["subject_commit_sha"] = "b" * 40
    result = evaluate_production_certification(
        payload,
        expected_commit_sha=COMMIT,
        expected_release_manifest_digest=MANIFEST_DIGEST,
        expected_repository="StrategyG/RusaAi",
        now=NOW,
    )
    assert result["status"] == "FAIL"
    assert any("backup_restore" in blocker for blocker in result["blockers"])


def test_certification_fails_when_release_manifest_digest_changes() -> None:
    payload = certification_record()
    payload["candidate"]["release_manifest_ref"]["digest"] = "sha256:" + "8" * 64
    result = evaluate_production_certification(
        payload,
        expected_commit_sha=COMMIT,
        expected_release_manifest_digest=MANIFEST_DIGEST,
        expected_repository="StrategyG/RusaAi",
        now=NOW,
    )
    assert result["status"] == "FAIL"
    assert any("release manifest digest" in blocker for blocker in result["blockers"])


def test_certification_holds_if_evidence_attestation_is_not_verified() -> None:
    payload = certification_record()
    payload["source"]["attestation_verified"] = False
    result = evaluate_production_certification(
        payload,
        expected_commit_sha=COMMIT,
        expected_release_manifest_digest=MANIFEST_DIGEST,
        expected_repository="StrategyG/RusaAi",
        now=NOW,
    )
    assert result["status"] == "HOLD"
    assert any("attestation" in blocker for blocker in result["blockers"])


def test_certification_fails_if_any_required_control_fails() -> None:
    payload = certification_record()
    payload["checks"]["tenant_isolation"]["status"] = "FAIL"
    result = evaluate_production_certification(
        payload,
        expected_commit_sha=COMMIT,
        expected_release_manifest_digest=MANIFEST_DIGEST,
        expected_repository="StrategyG/RusaAi",
        now=NOW,
    )
    assert result["status"] == "FAIL"
    assert any("tenant_isolation" in blocker for blocker in result["blockers"])


def test_certification_schema_rejects_missing_required_owner() -> None:
    payload = certification_record()
    payload["signoffs"].pop("process_owner")
    with pytest.raises(CertificationContractError, match="process_owner"):
        validate_production_certification(payload)
