"""Verify and safely extract the owner-supplied RUSA RAG source/agent-pack bundle.

The archive is treated as untrusted input. Verification is read-only by default;
--extract writes only the four pinned Markdown source files after every hash,
heading, profile and pack-frontmatter check succeeds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024
MAX_MEMBERS = 64
MAX_MEMBER_BYTES = 1 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_member(info: zipfile.ZipInfo) -> str:
    name = info.filename
    path = PurePosixPath(name)
    mode = info.external_attr >> 16
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or stat.S_ISLNK(mode)
        or info.file_size < 0
        or info.file_size > MAX_MEMBER_BYTES
    ):
        raise ValueError(f"unsafe or oversized ZIP member: {name!r}")
    return name


def _read_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        value = json.loads(archive.read(name).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid or missing JSON member: {name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON member must contain an object: {name}")
    return value


def _markdown_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_size = 0
    for index, line in enumerate(lines):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            token = fence.group(1)
            if fence_char is None:
                fence_char, fence_size = token[0], len(token)
            elif token[0] == fence_char and len(token) >= fence_size:
                fence_char, fence_size = None, 0
            continue
        if fence_char is not None:
            continue
        match = HEADING_RE.fullmatch(line.strip())
        if match:
            found.append((index, len(match.group(1)), line.strip()))
    return found


def _extract_sections(source: str, targets: list[str]) -> str:
    lines = source.splitlines()
    headings = _markdown_headings(lines)
    selected: list[tuple[int, int, str]] = []
    for target in targets:
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
        selected.append((start, end, target))
    selected.sort(key=lambda item: item[0])
    for previous, current in zip(selected, selected[1:]):
        if current[0] < previous[1]:
            raise ValueError("agent map contains overlapping source heading sections")
    sections = ["\n".join(lines[start:end]).strip() for start, end, _ in selected]
    if any(not section for section in sections):
        raise ValueError("agent source section is empty")
    return "\n\n".join(sections)


def _normalize(text: str) -> str:
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _chunk_count(text: str, size: int = 1200, overlap: int = 180) -> int:
    normalized = _normalize(text)
    count = 0
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + size, length)
        if end < length:
            candidates = (
                normalized.rfind("\n\n", start + size // 2, end),
                normalized.rfind("\n", start + size // 2, end),
                normalized.rfind(". ", start + size // 2, end),
                normalized.rfind(" ", start + size // 2, end),
            )
            boundary = max(candidates)
            if boundary > start:
                end = boundary + (2 if normalized[boundary : boundary + 2] == ". " else 1)
        if normalized[start:end].strip():
            count += 1
        if end >= length:
            break
        start = max(start + 1, end - overlap)
    return count


def _frontmatter(markdown: str) -> dict[str, str]:
    match = FRONTMATTER_RE.match(markdown)
    if not match:
        raise ValueError("agent pack is missing YAML frontmatter")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def verify_bundle(
    archive_path: str | Path,
    *,
    source_manifest_path: str | Path = "docs/knowledge-seed/manifest.json",
    agent_map_path: str | Path = "docs/knowledge-seed/agent_knowledge_map.json",
) -> dict[str, Any]:
    archive_path = Path(archive_path)
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("bundle must be a regular local file, not a symlink")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("bundle exceeds the 5 MiB archive limit")

    repo_manifest_path = Path(source_manifest_path)
    agent_map_file = Path(agent_map_path)
    for path, label in ((repo_manifest_path, "source manifest"), (agent_map_file, "agent map")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} must be a regular local file, not a symlink")

    repo_manifest = json.loads(repo_manifest_path.read_text(encoding="utf-8"))
    agent_map = json.loads(agent_map_file.read_text(encoding="utf-8"))
    repo_docs = {item["source_id"]: item for item in repo_manifest["documents"]}
    map_profiles = {item["agent_id"]: item for item in agent_map["profiles"]}

    if not zipfile.is_zipfile(archive_path):
        raise ValueError("bundle is not a valid ZIP archive")

    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ValueError("bundle contains too many ZIP members")
        names: set[str] = set()
        total_size = 0
        for info in infos:
            name = _safe_member(info)
            if name in names:
                raise ValueError(f"duplicate ZIP member: {name}")
            names.add(name)
            total_size += info.file_size
        if total_size > MAX_TOTAL_UNCOMPRESSED:
            raise ValueError("bundle exceeds the 2 MiB uncompressed size limit")
        if "source_manifest.json" not in names:
            raise ValueError("bundle is missing source_manifest.json")

        manifest = _read_json(archive, "source_manifest.json")
        if manifest.get("manifest_version") != "1.0.0":
            raise ValueError("unsupported bundle manifest version")
        if manifest.get("project_id") != repo_manifest.get("project_id"):
            raise ValueError("bundle project_id does not match the repository seed")
        source_items = manifest.get("source_documents")
        profiles = manifest.get("agent_profiles")
        if not isinstance(source_items, list) or len(source_items) != 4:
            raise ValueError("bundle must contain exactly four source document records")
        if not isinstance(profiles, list) or len(profiles) != 20:
            raise ValueError("bundle must contain exactly twenty agent profiles")

        source_texts: dict[str, str] = {}
        source_report = []
        for item in source_items:
            source_id = item.get("source_id")
            filename = item.get("filename")
            digest = item.get("sha256")
            if not isinstance(source_id, str) or not isinstance(filename, str):
                raise ValueError("source record has invalid identity fields")
            if Path(filename).name != filename or not filename.endswith(".md"):
                raise ValueError(f"unsafe source filename: {filename!r}")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise ValueError(f"invalid source SHA-256: {source_id}")
            member = f"sources/{filename}"
            if member not in names:
                raise ValueError(f"bundle is missing source file: {member}")
            raw = archive.read(member)
            if len(raw) != item.get("bytes") or _sha256(raw) != digest:
                raise ValueError(f"source size or SHA-256 mismatch: {source_id}")
            repo_item = repo_docs.get(source_id)
            if not repo_item or repo_item.get("filename") != filename or repo_item.get("sha256") != digest:
                raise ValueError(f"source record does not match repository manifest: {source_id}")
            source_texts[source_id] = raw.decode("utf-8")
            source_report.append({
                "source_id": source_id,
                "filename": filename,
                "bytes": len(raw),
                "sha256": digest,
                "chunk_count": _chunk_count(source_texts[source_id]),
                "verification_status": item.get("verification_status"),
                "implementation_status": item.get("implementation_status"),
            })

        if set(repo_docs) != {item["source_id"] for item in source_items}:
            raise ValueError("bundle source set does not match the repository manifest")
        if set(map_profiles) != {item.get("agent_id") for item in profiles}:
            raise ValueError("bundle's 20 agent profiles do not match the repository agent map")

        agent_report = []
        for profile in profiles:
            agent_id = profile.get("agent_id")
            if not isinstance(agent_id, str) or agent_id not in map_profiles:
                raise ValueError("agent profile has an unknown agent_id")
            mapped = map_profiles[agent_id]
            if (
                profile.get("source_id") != mapped.get("source_id")
                or profile.get("headings") != mapped.get("headings")
            ):
                raise ValueError(f"agent profile does not match curated map: {agent_id}")
            source_id = profile["source_id"]
            source_hash = next(item["sha256"] for item in source_items if item["source_id"] == source_id)
            if profile.get("source_sha256") != source_hash:
                raise ValueError(f"agent source hash mismatch: {agent_id}")
            section = _extract_sections(source_texts[source_id], profile["headings"])
            section_hash = _sha256(section.encode("utf-8"))
            if section_hash != profile.get("section_sha256"):
                raise ValueError(f"agent section SHA-256 mismatch: {agent_id}")
            pack_member = f"agent_packs/{agent_id}.md"
            if pack_member not in names:
                raise ValueError(f"bundle is missing agent pack: {pack_member}")
            pack = _frontmatter(archive.read(pack_member).decode("utf-8"))
            expected = {
                "agent_id": agent_id,
                "agent_name": profile.get("agent_name"),
                "source_id": source_id,
                "source_sha256": source_hash,
                "section_sha256": section_hash,
                "verification_status": profile.get("verification_status"),
                "implementation_status": profile.get("implementation_status"),
            }
            for key, value in expected.items():
                if pack.get(key) != str(value):
                    raise ValueError(f"agent pack frontmatter mismatch for {agent_id}: {key}")
            agent_report.append({
                "agent_id": agent_id,
                "source_id": source_id,
                "section_sha256": section_hash,
                "chunk_count": _chunk_count(section),
                "verification_status": profile.get("verification_status"),
                "implementation_status": profile.get("implementation_status"),
            })

    return {
        "status": "verified",
        "project_id": manifest["project_id"],
        "source_document_count": len(source_report),
        "source_chunk_count": sum(item["chunk_count"] for item in source_report),
        "agent_profile_count": len(agent_report),
        "agent_chunk_count": sum(item["chunk_count"] for item in agent_report),
        "source_documents": source_report,
        "agent_profiles": agent_report,
        "database_write": False,
        "embedding_calls": 0,
    }


def extract_sources(
    archive_path: str | Path,
    output_dir: str | Path,
    *,
    source_manifest_path: str | Path = "docs/knowledge-seed/manifest.json",
    agent_map_path: str | Path = "docs/knowledge-seed/agent_knowledge_map.json",
) -> dict[str, Any]:
    report = verify_bundle(
        archive_path,
        source_manifest_path=source_manifest_path,
        agent_map_path=agent_map_path,
    )
    output = Path(output_dir)
    if output.is_symlink():
        raise ValueError("source output directory must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    archive = zipfile.ZipFile(archive_path)
    try:
        manifest = _read_json(archive, "source_manifest.json")
        written = []
        for item in manifest["source_documents"]:
            target = output / item["filename"]
            if target.exists() and target.is_symlink():
                raise ValueError(f"refusing to replace symlink source: {target.name}")
            data = archive.read(f"sources/{item['filename']}")
            if _sha256(data) != item["sha256"]:
                raise ValueError(f"source changed during extraction: {item['source_id']}")
            target.write_bytes(data)
            written.append(str(target))
    finally:
        archive.close()
    report["extracted_files"] = written
    report["extraction_performed"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rusa-knowledge-bundle",
        description="Verify the owner-supplied RUSA RAG/20-agent ZIP; extraction is opt-in.",
    )
    parser.add_argument("--archive", required=True, help="path to RUSA_AI_RAG_Source_and_20_Agent_Packs.zip")
    parser.add_argument("--source-manifest", default="docs/knowledge-seed/manifest.json")
    parser.add_argument("--agent-map", default="docs/knowledge-seed/agent_knowledge_map.json")
    parser.add_argument("--output-dir", default="docs/knowledge-seed/sources")
    parser.add_argument("--extract", action="store_true", help="extract four pinned source files after full verification")
    args = parser.parse_args(argv)
    try:
        if args.extract:
            report = extract_sources(
                args.archive,
                args.output_dir,
                source_manifest_path=args.source_manifest,
                agent_map_path=args.agent_map,
            )
        else:
            report = verify_bundle(
                args.archive,
                source_manifest_path=args.source_manifest,
                agent_map_path=args.agent_map,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"RUSA knowledge bundle validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
