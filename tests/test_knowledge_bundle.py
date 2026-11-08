import hashlib
import json
import zipfile

import pytest

from rusa_knowledge import bundle


def _write_fixture(tmp_path):
    sources = {
        "RUSA-SRC-001": ("source-1.md", "\n".join(f"# Heading {i}\n\nSource {i} facts." for i in range(5))),
        "RUSA-SRC-002": ("source-2.md", "\n".join(f"# Heading {i+5}\n\nSource {i+5} facts." for i in range(5))),
        "RUSA-SRC-003": ("source-3.md", "\n".join(f"# Heading {i+10}\n\nSource {i+10} facts." for i in range(5))),
        "RUSA-SRC-004": ("source-4.md", "\n".join(f"# Heading {i+15}\n\nSource {i+15} facts." for i in range(5))),
    }
    source_records = []
    repo_documents = []
    for source_id, (filename, text) in sources.items():
        raw = text.encode()
        digest = hashlib.sha256(raw).hexdigest()
        source_records.append({
            "source_id": source_id,
            "filename": filename,
            "sha256": digest,
            "bytes": len(raw),
            "verification_status": "UNVERIFIED",
            "implementation_status": "NOT_VERIFIED",
        })
        repo_documents.append({"source_id": source_id, "filename": filename, "sha256": digest})

    profiles = []
    map_profiles = []
    packs = {}
    for index in range(20):
        source_index = index // 5
        source_id = list(sources)[source_index]
        filename, source_text = sources[source_id]
        heading = f"# Heading {index}"
        section = bundle._extract_sections(source_text, [heading])
        section_hash = hashlib.sha256(section.encode()).hexdigest()
        agent_id = f"agent-{index:02d}"
        agent_name = f"Agent {index:02d}"
        source_hash = source_records[source_index]["sha256"]
        profile = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "source_id": source_id,
            "headings": [heading],
            "source_sha256": source_hash,
            "section_sha256": section_hash,
            "verification_status": "UNVERIFIED",
            "implementation_status": "NOT_VERIFIED",
        }
        profiles.append(profile)
        map_profiles.append({
            "agent_id": agent_id,
            "agent_name": agent_name,
            "source_id": source_id,
            "headings": [heading],
            "knowledge_domains": ["testing"],
        })
        packs[f"agent_packs/{agent_id}.md"] = (
            "---\n"
            f"agent_id: {agent_id}\n"
            f"agent_name: {agent_name}\n"
            f"source_id: {source_id}\n"
            f"source_sha256: {source_hash}\n"
            f"section_sha256: {section_hash}\n"
            "verification_status: UNVERIFIED\n"
            "implementation_status: NOT_VERIFIED\n"
            "---\n\n"
            f"{section}\n"
        )

    bundle_manifest = {
        "manifest_version": "1.0.0",
        "project_id": "rusa-ai",
        "source_documents": source_records,
        "agent_profiles": profiles,
    }
    repo_manifest = {
        "manifest_version": "1.0.0",
        "project_id": "rusa-ai",
        "collection": "agent",
        "documents": repo_documents,
    }
    agent_map = {
        "schema_version": "1.0.0",
        "project_id": "rusa-ai",
        "profiles": map_profiles,
    }
    manifest_path = tmp_path / "manifest.json"
    map_path = tmp_path / "agent-map.json"
    manifest_path.write_text(json.dumps(repo_manifest), encoding="utf-8")
    map_path.write_text(json.dumps(agent_map), encoding="utf-8")
    archive_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("source_manifest.json", json.dumps(bundle_manifest))
        archive.writestr("README.txt", "test fixture")
        for source_id, (filename, text) in sources.items():
            archive.writestr(f"sources/{filename}", text)
        for name, text in packs.items():
            archive.writestr(name, text)
    return archive_path, manifest_path, map_path


def test_verify_and_extract_owner_bundle(tmp_path):
    archive, manifest, agent_map = _write_fixture(tmp_path)
    report = bundle.verify_bundle(
        archive,
        source_manifest_path=manifest,
        agent_map_path=agent_map,
    )
    assert report["status"] == "verified"
    assert report["source_document_count"] == 4
    assert report["agent_profile_count"] == 20
    assert report["database_write"] is False
    assert report["embedding_calls"] == 0

    output = tmp_path / "sources"
    extracted = bundle.extract_sources(
        archive,
        output,
        source_manifest_path=manifest,
        agent_map_path=agent_map,
    )
    assert extracted["extraction_performed"] is True
    assert len(list(output.glob("*.md"))) == 4


def test_verify_bundle_rejects_duplicate_source_member(tmp_path):
    archive, manifest, agent_map = _write_fixture(tmp_path)
    with zipfile.ZipFile(archive, "a") as z:
        z.writestr("sources/source-1.md", "# altered source")
    with pytest.raises(ValueError, match="duplicate ZIP member"):
        bundle.verify_bundle(
            archive,
            source_manifest_path=manifest,
            agent_map_path=agent_map,
        )


def test_safe_member_rejects_traversal_and_symlinks():
    traversal = zipfile.ZipInfo("../escape.md")
    with pytest.raises(ValueError, match="unsafe"):
        bundle._safe_member(traversal)

    symlink = zipfile.ZipInfo("sources/link.md")
    symlink.external_attr = (0o120777 << 16)
    with pytest.raises(ValueError, match="unsafe"):
        bundle._safe_member(symlink)


def test_extract_sections_ignores_headings_inside_fenced_code():
    text = "## Real section\n\n~~~md\n## Not a heading\n~~~\n\nText."
    section = bundle._extract_sections(text, ["## Real section"])
    assert "## Not a heading" in section
    with pytest.raises(ValueError, match="found 0"):
        bundle._extract_sections(text, ["## Not a heading"])
