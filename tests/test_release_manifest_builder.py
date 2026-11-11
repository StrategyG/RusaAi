from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rusa_agents.certification import manifest_builder

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
COMMIT = "a" * 40
OWNER_REPOSITORY = "StrategyG/RusaAi"
IDENTITY = "https://github.com/StrategyG/RusaAi/.github/workflows/release.yml@refs/tags/v1.1.0"
ISSUER = "https://token.actions.githubusercontent.com"


def _write_json(path: Path, payload: dict[str, object]) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _artifact_inputs(tmp_path: Path, high: bool = False) -> dict[str, dict[str, str]]:
    artifacts = {}
    for index, name in enumerate(("api", "worker", "sandbox", "web"), start=1):
        scan = {
            "Results": [
                {
                    "Vulnerabilities": (
                        [{"VulnerabilityID": "CVE-2026-1000", "Severity": "HIGH"}]
                        if high and name == "api"
                        else []
                    )
                }
            ]
        }
        scan_path = _write_json(tmp_path / f"scan-{name}.json", scan)
        sbom_path = tmp_path / f"sbom-{name}.cdx.json"
        sbom_path.write_text(json.dumps({"bomFormat": "CycloneDX", "serialNumber": name}), encoding="utf-8")
        artifacts[name] = {
            "image": f"ghcr.io/strategyg/rusa-ai-{name}",
            "digest": "sha256:" + str(index) * 64,
            "scan_json": scan_path,
            "sbom": str(sbom_path),
        }
    return artifacts


def _mock_verifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manifest_builder,
        "verify_cosign_image_signature",
        lambda **kwargs: {
            "verified": True,
            "signer_identity": kwargs["certificate_identity"],
            "oidc_issuer": kwargs["oidc_issuer"],
            "subject_digest": kwargs["digest"],
        },
    )
    monkeypatch.setattr(
        manifest_builder,
        "verify_github_slsa_provenance",
        lambda **kwargs: {
            "verified": True,
            "repository": kwargs["repository"],
            "source_digest": kwargs["source_digest"],
            "predicate_type": "https://slsa.dev/provenance/v1",
        },
    )


def _build(tmp_path: Path, artifacts: dict[str, dict[str, str]]) -> dict[str, object]:
    db_metadata = _write_json(
        tmp_path / "trivy-db-metadata.json",
        {
            "Version": 2,
            "UpdatedAt": "2026-11-11T11:00:00Z",
            "NextUpdate": "2026-11-11T23:00:00Z",
            "DownloadedAt": "2026-11-11T11:01:00Z",
        },
    )
    return manifest_builder.build_release_manifest(
        repository=OWNER_REPOSITORY,
        commit_sha=COMMIT,
        release_id="v1.1.0",
        source_ref="refs/tags/v1.1.0",
        workflow_run_id=123456,
        workflow_path=".github/workflows/release.yml",
        signer_workflow=".github/workflows/release.yml",
        certificate_identity=IDENTITY,
        oidc_issuer=ISSUER,
        trivy_db_metadata=db_metadata,
        sbom_base_uri="https://github.com/StrategyG/RusaAi/releases/download/v1.1.0",
        artifacts=artifacts,
        now=NOW,
    )


def test_manifest_builder_binds_four_verified_images_and_sboms(tmp_path, monkeypatch):
    _mock_verifiers(monkeypatch)
    manifest = _build(tmp_path, _artifact_inputs(tmp_path))
    assert manifest["schema_version"] == "1.1"
    assert {artifact["name"] for artifact in manifest["artifacts"]} == {
        "api",
        "worker",
        "sandbox",
        "web",
    }
    assert all(artifact["signature"]["verified"] for artifact in manifest["artifacts"])
    assert all(artifact["provenance"]["verified"] for artifact in manifest["artifacts"])
    assert all(artifact["sbom"]["subject_digest"] == artifact["digest"] for artifact in manifest["artifacts"])


def test_manifest_builder_blocks_unapproved_high_findings(tmp_path, monkeypatch):
    _mock_verifiers(monkeypatch)
    with pytest.raises(manifest_builder.CertificationContractError, match="BLOCKED"):
        _build(tmp_path, _artifact_inputs(tmp_path, high=True))


def test_manifest_builder_requires_trivy_database_timestamp(tmp_path, monkeypatch):
    _mock_verifiers(monkeypatch)
    artifacts = _artifact_inputs(tmp_path)
    db_metadata = _write_json(tmp_path / "bad-db-metadata.json", {"Version": 2})
    with pytest.raises(ValueError, match="UpdatedAt"):
        manifest_builder.build_release_manifest(
            repository=OWNER_REPOSITORY,
            commit_sha=COMMIT,
            release_id="v1.1.0",
            source_ref="refs/tags/v1.1.0",
            workflow_run_id=123456,
            workflow_path=".github/workflows/release.yml",
            signer_workflow=".github/workflows/release.yml",
            certificate_identity=IDENTITY,
            oidc_issuer=ISSUER,
            trivy_db_metadata=db_metadata,
            sbom_base_uri="https://github.com/StrategyG/RusaAi/releases/download/v1.1.0",
            artifacts=artifacts,
            now=NOW,
        )
