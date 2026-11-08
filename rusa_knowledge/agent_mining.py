"""Curated, source-pinned Markdown section mining for agent-specific RAG packs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import chunk_text, normalize_text
from .safety import reject_credential_material, validate_document_metadata
from .seed import (
    SLUG_PATTERN,
    SOURCE_ID_PATTERN,
    TENANT_PATTERN,
    prepare_seed_documents,
)
from .store import COLLECTION_PATTERN


MAP_VERSION = "1.0.0"
MAX_MAP_BYTES = 1_000_000
HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


@dataclass(frozen=True)
class PreparedAgentKnowledge:
    agent_id: str
    agent_name: str
    source_id: str
    title: str
    source_uri: str
    content: str
    metadata: dict[str, Any]
    section_sha256: str
    chunk_count: int


def load_agent_map(map_path: str | Path) -> tuple[dict[str, Any], str]:
    """Load a bounded JSON map; the map is configuration, not executable policy."""
    path = Path(map_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("agent map must be a regular local file, not a symlink")
    if path.stat().st_size < 1 or path.stat().st_size > MAX_MAP_BYTES:
        raise ValueError("agent map must contain between 1 byte and 1 MiB")
    raw = path.read_bytes()
    try:
        mapping = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("agent map must be valid UTF-8 JSON") from exc
    if not isinstance(mapping, dict) or mapping.get("schema_version") != MAP_VERSION:
        raise ValueError(f"agent map schema_version must be {MAP_VERSION}")
    profiles = mapping.get("profiles")
    if not isinstance(profiles, list) or not profiles or len(profiles) > 100:
        raise ValueError("agent map profiles must contain between 1 and 100 entries")

    seen_agents: set[str] = set()
    for index, profile in enumerate(profiles):
        prefix = f"profiles[{index}]"
        if not isinstance(profile, dict):
            raise ValueError(f"{prefix} must be an object")
        agent_id = profile.get("agent_id")
        if (
            not isinstance(agent_id, str)
            or not SLUG_PATTERN.fullmatch(agent_id)
            or agent_id in seen_agents
        ):
            raise ValueError(f"{prefix}.agent_id is invalid or duplicated")
        seen_agents.add(agent_id)
        agent_name = profile.get("agent_name")
        if not isinstance(agent_name, str) or not agent_name.strip() or len(agent_name) > 128:
            raise ValueError(f"{prefix}.agent_name must be a bounded non-empty string")
        source_id = profile.get("source_id")
        if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ValueError(f"{prefix}.source_id is invalid")
        headings = profile.get("headings")
        if (
            not isinstance(headings, list)
            or not headings
            or len(headings) > 20
            or any(
                not isinstance(heading, str)
                or not HEADING_PATTERN.fullmatch(heading)
                for heading in headings
            )
            or len(set(headings)) != len(headings)
        ):
            raise ValueError(f"{prefix}.headings must be unique Markdown headings")
        domains = profile.get("knowledge_domains")
        if (
            not isinstance(domains, list)
            or not domains
            or len(domains) > 50
            or any(not isinstance(domain, str) or not SLUG_PATTERN.fullmatch(domain) for domain in domains)
            or len(set(domains)) != len(domains)
        ):
            raise ValueError(f"{prefix}.knowledge_domains must be unique domain slugs")

    project_id = mapping.get("project_id")
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project_id):
        raise ValueError("agent map project_id is invalid")
    return mapping, hashlib.sha256(raw).hexdigest()


def _markdown_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return (zero-based line, heading level, exact source line), ignoring fenced code."""
    headings: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_size = 0
    for index, line in enumerate(lines):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            token = fence.group(1)
            if fence_char is None:
                fence_char = token[0]
                fence_size = len(token)
            elif token[0] == fence_char and len(token) >= fence_size:
                fence_char = None
                fence_size = 0
            continue
        if fence_char is not None:
            continue
        match = HEADING_PATTERN.fullmatch(line.strip())
        if match:
            headings.append((index, len(match.group(1)), line.strip()))
    return headings


def extract_heading_sections(
    content: str,
    target_headings: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    """Extract exact heading subtrees and preserve their original line locators."""
    lines = content.splitlines()
    headings = _markdown_headings(lines)
    selected: list[tuple[int, int, int, str]] = []

    for target in target_headings:
        matches = [item for item in headings if item[2] == target]
        if len(matches) != 1:
            raise ValueError(
                f"source heading must exist exactly once: {target!r} (found {len(matches)})"
            )
        start, level, _ = matches[0]
        end = len(lines)
        for next_start, next_level, _ in headings:
            if next_start > start and next_level <= level:
                end = next_start
                break
        if end <= start:
            raise ValueError(f"source heading has an empty section: {target!r}")
        selected.append((start, end, level, target))

    selected.sort(key=lambda item: item[0])
    for previous, current in zip(selected, selected[1:]):
        if current[0] < previous[1]:
            raise ValueError("agent map contains overlapping source heading sections")

    sections: list[str] = []
    locators: list[dict[str, Any]] = []
    for start, end, _, target in selected:
        section = "\n".join(lines[start:end]).strip()
        if not section:
            raise ValueError(f"source section is empty: {target!r}")
        sections.append(section)
        locators.append({
            "heading": target,
            "start_line": start + 1,
            "end_line": end,
        })
    return "\n\n".join(sections), locators


def prepare_agent_knowledge(
    manifest_path: str | Path,
    source_dir: str | Path,
    agent_map_path: str | Path,
) -> tuple[dict[str, Any], list[PreparedAgentKnowledge]]:
    """Verify original source hashes and build one scoped document per mapped agent."""
    manifest, source_documents = prepare_seed_documents(manifest_path, source_dir)
    mapping, map_sha256 = load_agent_map(agent_map_path)
    if mapping["project_id"] != manifest["project_id"]:
        raise ValueError("agent map project_id must match the pinned source manifest")
    root = Path(source_dir).resolve(strict=True)
    source_by_id = {item.source_id: item for item in source_documents}
    source_manifest = {item["source_id"]: item for item in manifest["documents"]}
    prepared: list[PreparedAgentKnowledge] = []

    for profile in mapping["profiles"]:
        source_id = profile["source_id"]
        source = source_by_id.get(source_id)
        source_meta = source_manifest.get(source_id)
        if source is None or source_meta is None:
            raise ValueError(f"agent map references unknown source_id: {source_id}")
        source_path = root / source_meta["filename"]
        if source_path.is_symlink() or not source_path.is_file():
            raise ValueError(f"source '{source_id}' must remain a regular file, not a symlink")
        if source_path.resolve(strict=True).parent != root:
            raise ValueError(f"source '{source_id}' escapes the configured source directory")
        raw_source = source_path.read_bytes()
        if hashlib.sha256(raw_source).hexdigest() != source.source_sha256:
            raise ValueError(f"source '{source_id}' changed after source-manifest verification")
        try:
            raw_content = raw_source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"source '{source_id}' is not valid UTF-8 Markdown") from exc
        section_content, locators = extract_heading_sections(
            raw_content, profile["headings"]
        )
        normalized = normalize_text(section_content)
        reject_credential_material(normalized)
        section_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        source_uri = f"rusa://agent-knowledge/{profile['agent_id']}/{source_id}"
        title = f"{profile['agent_name']} — {source.title}"
        metadata = {
            "source_id": source_id,
            "source_filename": source_meta["filename"],
            "source_sha256": source.source_sha256,
            "source_version": source_meta["source_version"],
            "source_date": source_meta["source_date"],
            "source_status": source_meta["source_status"],
            "source_status_class": source_meta["source_status_class"],
            "verification_status": source_meta["verification_status"],
            "implementation_status": source_meta["implementation_status"],
            "knowledge_domain": source_meta["knowledge_domain"],
            "knowledge_type": "agent_profile_sections",
            "knowledge_domains": profile["knowledge_domains"],
            "agent_ids": [profile["agent_id"]],
            "agent_names": [profile["agent_name"]],
            "knowledge_scope": "agent_specific",
            "source_section_paths": profile["headings"],
            "source_locators": locators,
            "source_section_sha256": section_sha256,
            "agent_map_sha256": map_sha256,
            "mining_profile": mapping.get("mining_profile", "rusa-agent-section-mining"),
            "chunking_strategy": "character_overlap_v1",
        }
        validate_document_metadata(source_uri, title, metadata)
        chunks = chunk_text(normalized)
        if not chunks:
            raise ValueError(f"agent profile '{profile['agent_id']}' produced no searchable chunks")
        prepared.append(
            PreparedAgentKnowledge(
                agent_id=profile["agent_id"],
                agent_name=profile["agent_name"],
                source_id=source_id,
                title=title,
                source_uri=source_uri,
                content=normalized,
                metadata=metadata,
                section_sha256=section_sha256,
                chunk_count=len(chunks),
            )
        )
    return manifest, prepared


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rusa_knowledge.agent_mining",
        description="Mine exact source sections into agent-specific RUSA knowledge documents.",
    )
    parser.add_argument(
        "--manifest",
        default="docs/knowledge-seed/manifest.json",
        help="pinned source manifest",
    )
    parser.add_argument(
        "--source-dir",
        default=None,
        help="directory containing the exact original source files",
    )
    parser.add_argument(
        "--agent-map",
        default="docs/knowledge-seed/agent_knowledge_map.json",
        help="curated agent-to-source-heading map",
    )
    parser.add_argument("--tenant-id", help="existing tenant authorized to hold RUSA knowledge")
    parser.add_argument("--collection", help="override the source manifest collection")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="embed and write agent-specific documents; default is a non-writing preflight",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = Path(args.manifest)
    source_dir = Path(args.source_dir) if args.source_dir else manifest_path.parent / "sources"
    service = None
    try:
        manifest, documents = prepare_agent_knowledge(
            manifest_path, source_dir, args.agent_map
        )
        collection = args.collection or manifest["collection"]
        if not isinstance(collection, str) or not COLLECTION_PATTERN.fullmatch(collection):
            raise ValueError("collection is invalid")
        if not args.apply:
            print(json.dumps({
                "mode": "dry_run",
                "project_id": manifest["project_id"],
                "collection": collection,
                "agent_profile_count": len(documents),
                "document_count": len(documents),
                "chunk_count": sum(item.chunk_count for item in documents),
                "documents": [
                    {
                        "agent_id": item.agent_id,
                        "agent_name": item.agent_name,
                        "source_id": item.source_id,
                        "source_uri": item.source_uri,
                        "source_section_sha256": item.section_sha256,
                        "chunk_count": item.chunk_count,
                        "source_locators": item.metadata["source_locators"],
                    }
                    for item in documents
                ],
                "database_write": False,
                "embedding_calls": 0,
            }, ensure_ascii=False, indent=2))
            return 0

        if not args.tenant_id or not TENANT_PATTERN.fullmatch(args.tenant_id):
            raise ValueError("--apply requires a valid existing --tenant-id")
        database_url = os.getenv("RUSA_KNOWLEDGE_DATABASE_URL")
        if not database_url:
            raise RuntimeError("RUSA_KNOWLEDGE_DATABASE_URL is required; refusing application-database fallback")
        if not os.getenv("RUSA_KNOWLEDGE_DB_ROLE"):
            raise RuntimeError("RUSA_KNOWLEDGE_DB_ROLE must name the restricted knowledge database role")

        from .service import KnowledgeService

        service = KnowledgeService.from_env(check_ready=True, database_url=database_url)
        results = []
        for item in documents:
            result = service.ingest_text(
                tenant_id=args.tenant_id,
                collection=collection,
                source_uri=item.source_uri,
                title=item.title,
                content=item.content,
                metadata=item.metadata,
            )
            results.append({
                "agent_id": item.agent_id,
                "source_id": item.source_id,
                "document_id": result["document_id"],
                "source_uri": result["source_uri"],
                "chunk_count": result["chunk_count"],
                "content_hash": result["content_hash"],
            })
        print(json.dumps({
            "mode": "applied",
            "project_id": manifest["project_id"],
            "tenant_id": args.tenant_id,
            "collection": collection,
            "agent_profile_count": len(results),
            "document_count": len(results),
            "chunk_count": sum(item["chunk_count"] for item in results),
            "results": results,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"RUSA agent knowledge mining failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
