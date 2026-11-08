import hashlib
import json
from pathlib import Path

import pytest

from rusa_knowledge.seed import load_manifest, main, prepare_seed_documents

ROOT = Path(__file__).resolve().parents[1]
SEED_MANIFEST = ROOT / "docs" / "knowledge-seed" / "manifest.json"


def _write_fixture(tmp_path: Path, content: str = "# RUSA Test\n\nA source document.\n"):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    filename = "test-source.md"
    source_path = source_dir / filename
    source_path.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": "1.0.0",
        "project_id": "rusa-ai",
        "collection": "agent",
        "ingestion_profile": "test-seed-v1",
        "documents": [
            {
                "source_id": "RUSA-TEST-001",
                "filename": filename,
                "title": "RUSA Test",
                "knowledge_domain": "architecture",
                "knowledge_type": "architecture_overview",
                "source_version": "1.0",
                "source_date": None,
                "source_status": None,
                "source_status_class": "UNSPECIFIED",
                "verification_status": "UNVERIFIED",
                "implementation_status": "NOT_VERIFIED",
                "agent_ids": [],
                "agent_names": [],
                "sha256": digest,
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, source_dir, manifest


def test_committed_manifest_pins_four_sources_and_preserves_statuses():
    manifest = load_manifest(SEED_MANIFEST)
    assert manifest["project_id"] == "rusa-ai"
    assert manifest["collection"] == "agent"
    assert len(manifest["documents"]) == 4
    assert {
        item["source_id"] for item in manifest["documents"]
    } == {"RUSA-ARCH-001", "RUSA-AGENT-001", "RUSA-COMM-001", "RUSA-DEVOPS-001"}
    assert all(item["verification_status"] == "UNVERIFIED" for item in manifest["documents"])
    assert all(item["implementation_status"] == "NOT_VERIFIED" for item in manifest["documents"])
    assert manifest["documents"][2]["source_status"] == "Diusulkan"
    assert manifest["documents"][2]["source_status_class"] == "PROPOSED"
    assert manifest["documents"][3]["source_status"] == "Proposed Architecture"
    assert manifest["documents"][3]["source_status_class"] == "PROPOSED"


def test_prepare_seed_documents_checks_hash_and_builds_provenance(tmp_path):
    manifest_path, source_dir, _ = _write_fixture(tmp_path)
    manifest, prepared = prepare_seed_documents(manifest_path, source_dir)

    assert manifest["project_id"] == "rusa-ai"
    assert len(prepared) == 1
    item = prepared[0]
    assert item.source_id == "RUSA-TEST-001"
    assert item.source_uri == "rusa://knowledge-source/RUSA-TEST-001"
    assert item.chunk_count == 1
    assert item.metadata["source_sha256"] == item.source_sha256
    assert item.metadata["verification_status"] == "UNVERIFIED"
    assert item.metadata["implementation_status"] == "NOT_VERIFIED"
    assert item.metadata["chunking_strategy"] == "character_overlap_v1"


def test_prepare_seed_documents_rejects_source_hash_mismatch(tmp_path):
    manifest_path, source_dir, _ = _write_fixture(tmp_path)
    (source_dir / "test-source.md").write_text("# Changed after manifest\n", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        prepare_seed_documents(manifest_path, source_dir)


def test_manifest_rejects_path_traversal(tmp_path):
    manifest_path, _, manifest = _write_fixture(tmp_path)
    manifest["documents"][0]["filename"] = "../outside.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Markdown basename"):
        load_manifest(manifest_path)


def test_manifest_rejects_duplicate_source_ids(tmp_path):
    manifest_path, _, manifest = _write_fixture(tmp_path)
    duplicate = dict(manifest["documents"][0])
    duplicate["filename"] = "another-source.md"
    manifest["documents"].append(duplicate)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source_id is invalid or duplicated"):
        load_manifest(manifest_path)


def test_prepare_seed_documents_rejects_symlink_sources(tmp_path):
    manifest_path, source_dir, _ = _write_fixture(tmp_path)
    original = source_dir / "test-source.md"
    target = source_dir / "real-source.md"
    original.rename(target)
    original.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        prepare_seed_documents(manifest_path, source_dir)


def test_prepare_seed_documents_blocks_credentials_before_ingestion(tmp_path):
    secret_text = (
        "# Internal\n\n-----BEGIN PRIVATE KEY-----\n"
        + ("A" * 64)
        + "\n-----END PRIVATE KEY-----\n"
    )
    manifest_path, source_dir, _ = _write_fixture(tmp_path, secret_text)

    with pytest.raises(ValueError, match="private-key material"):
        prepare_seed_documents(manifest_path, source_dir)


def test_default_mode_is_non_writing_preflight(tmp_path, capsys):
    manifest_path, source_dir, _ = _write_fixture(tmp_path)

    result = main(["--manifest", str(manifest_path), "--source-dir", str(source_dir)])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["mode"] == "dry_run"
    assert payload["document_count"] == 1
    assert payload["chunk_count"] == 1
    assert payload["database_write"] is False
    assert payload["embedding_calls"] == 0


def test_apply_requires_an_explicit_tenant_and_dedicated_database(tmp_path, monkeypatch, capsys):
    manifest_path, source_dir, _ = _write_fixture(tmp_path)
    monkeypatch.delenv("RUSA_KNOWLEDGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("RUSA_KNOWLEDGE_DB_ROLE", raising=False)

    missing_tenant = main(
        ["--manifest", str(manifest_path), "--source-dir", str(source_dir), "--apply"]
    )
    assert missing_tenant == 1
    assert "--tenant-id" in capsys.readouterr().err

    missing_database = main(
        [
            "--manifest", str(manifest_path),
            "--source-dir", str(source_dir),
            "--tenant-id", "rusa-platform",
            "--apply",
        ]
    )
    assert missing_database == 1
    assert "RUSA_KNOWLEDGE_DATABASE_URL is required" in capsys.readouterr().err
