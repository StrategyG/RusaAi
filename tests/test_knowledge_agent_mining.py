import hashlib
import json
from pathlib import Path

import pytest

from rusa_knowledge.agent_mining import (
    extract_heading_sections,
    load_agent_map,
    main,
    prepare_agent_knowledge,
)


def _write_sources(tmp_path: Path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    content = """# RUSA Test Source

## Agent One

Agent one objective and responsibilities.

### Inputs

- Request A
- Context B

## Agent Two

Agent two private procedure.

## Shared Contract

Messages use a stable schema.
"""
    source_file = source_dir / "test-source.md"
    source_file.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": "1.0.0",
        "project_id": "rusa-ai",
        "collection": "agent",
        "ingestion_profile": "agent-mining-test-v1",
        "documents": [{
            "source_id": "RUSA-TEST-001",
            "filename": "test-source.md",
            "title": "RUSA Test Source",
            "knowledge_domain": "agent_contract",
            "knowledge_type": "test_source",
            "source_version": "1.0",
            "source_date": None,
            "source_status": None,
            "source_status_class": "UNSPECIFIED",
            "verification_status": "UNVERIFIED",
            "implementation_status": "NOT_VERIFIED",
            "agent_ids": [],
            "agent_names": [],
            "knowledge_scope": "shared",
            "sha256": digest,
        }],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    agent_map = {
        "schema_version": "1.0.0",
        "project_id": "rusa-ai",
        "mining_profile": "test-agent-section-mining-v1",
        "profiles": [{
            "agent_id": "test-agent",
            "agent_name": "Test Agent",
            "source_id": "RUSA-TEST-001",
            "headings": ["## Agent One"],
            "knowledge_domains": ["agent_contract", "procedure"],
        }],
    }
    map_path = tmp_path / "agent-map.json"
    map_path.write_text(json.dumps(agent_map), encoding="utf-8")
    return manifest_path, source_dir, map_path


def test_prepare_agent_knowledge_extracts_exact_heading_subtree_and_provenance(tmp_path):
    manifest_path, source_dir, map_path = _write_sources(tmp_path)
    manifest, documents = prepare_agent_knowledge(
        manifest_path, source_dir, map_path
    )

    assert manifest["project_id"] == "rusa-ai"
    assert len(documents) == 1
    item = documents[0]
    assert item.agent_id == "test-agent"
    assert item.source_uri == "rusa://agent-knowledge/test-agent/RUSA-TEST-001"
    assert "Agent one objective" in item.content
    assert "Inputs" in item.content
    assert "Agent two private procedure" not in item.content
    assert "Shared Contract" not in item.content
    assert item.metadata["knowledge_scope"] == "agent_specific"
    assert item.metadata["agent_ids"] == ["test-agent"]
    assert item.metadata["source_id"] == "RUSA-TEST-001"
    assert item.metadata["source_section_paths"] == ["## Agent One"]
    assert item.metadata["source_locators"] == [{
        "heading": "## Agent One",
        "start_line": 3,
        "end_line": 11,
    }]
    assert len(item.section_sha256) == 64
    assert item.chunk_count >= 1


def test_extract_heading_sections_ignores_headings_inside_fenced_code():
    content = """# Guide

```markdown
## Hidden Heading
not a real section
```

## Visible Heading

Real section.
"""
    extracted, locators = extract_heading_sections(
        content, ["## Visible Heading"]
    )
    assert "Real section" in extracted
    assert "Hidden Heading" not in extracted
    assert locators[0]["start_line"] == 8


def test_extract_heading_sections_rejects_missing_or_ambiguous_headings():
    with pytest.raises(ValueError, match="found 0"):
        extract_heading_sections("# Guide\n\n## Present\nText", ["## Missing"])
    with pytest.raises(ValueError, match="found 2"):
        extract_heading_sections(
            "# Guide\n\n## Repeated\nA\n\n## Repeated\nB",
            ["## Repeated"],
        )


def test_agent_map_rejects_duplicate_agent_ids(tmp_path):
    _, _, map_path = _write_sources(tmp_path)
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    mapping["profiles"].append(dict(mapping["profiles"][0]))
    map_path.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(ValueError, match="agent_id is invalid or duplicated"):
        load_agent_map(map_path)


def test_agent_mining_dry_run_does_not_require_database_or_embedding(tmp_path, capsys):
    manifest_path, source_dir, map_path = _write_sources(tmp_path)
    exit_code = main([
        "--manifest", str(manifest_path),
        "--source-dir", str(source_dir),
        "--agent-map", str(map_path),
    ])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run"
    assert output["agent_profile_count"] == 1
    assert output["database_write"] is False
    assert output["embedding_calls"] == 0


def test_curated_agent_map_covers_twenty_distinct_profiles_and_source_groups():
    mapping, map_sha256 = load_agent_map(
        Path("docs/knowledge-seed/agent_knowledge_map.json")
    )
    profiles = mapping["profiles"]
    assert len(profiles) == 20
    assert len({profile["agent_id"] for profile in profiles}) == 20
    assert len(map_sha256) == 64
    assert sum(profile["source_id"] == "RUSA-AGENT-001" for profile in profiles) == 10
    assert sum(profile["source_id"] == "RUSA-DEVOPS-001" for profile in profiles) == 10
    assert {
        profile["agent_id"] for profile in profiles
    } >= {
        "supreme-orchestrator",
        "system-architect",
        "memory-knowledge",
        "rusa_plan_agent",
        "rusa_security_guard_agent",
    }
