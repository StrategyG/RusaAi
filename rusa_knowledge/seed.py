"""Pinned, fail-closed seed ingestion for the initial RUSA architecture/agent corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    chunk_text,
    normalize_text,
)
from .parsers import MAX_DOCUMENT_BYTES, parse_file
from .safety import reject_credential_material, validate_document_metadata
from .store import COLLECTION_PATTERN, TENANT_PATTERN

MANIFEST_VERSION = "1.0.0"
MAX_MANIFEST_BYTES = 1_000_000
SOURCE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SOURCE_STATUS_CLASSES = {"PROPOSED", "APPROVED", "DEPRECATED", "UNSPECIFIED"}
VERIFICATION_STATUSES = {"VERIFIED", "UNVERIFIED", "CONFLICTED"}
IMPLEMENTATION_STATUSES = {"IMPLEMENTED", "VERIFIED", "NOT_VERIFIED"}


@dataclass(frozen=True)
class PreparedSeedDocument:
    source_id: str
    title: str
    source_uri: str
    content: str
    metadata: dict[str, Any]
    source_sha256: str
    chunk_count: int


def _require_string(value: Any, field: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"manifest field '{field}' must be a non-empty string of at most {max_length} characters")
    return value.strip()


def load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Load and strictly validate the public manifest without reading source documents."""
    path = Path(manifest_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("manifest must be a regular local file, not a symlink")
    if path.stat().st_size < 1 or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest must contain between 1 byte and 1 MiB")

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest must be valid UTF-8 JSON") from exc

    if not isinstance(manifest, dict):
        raise ValueError("manifest root must be a JSON object")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(f"manifest_version must be {MANIFEST_VERSION}")
    project_id = _require_string(manifest.get("project_id"), "project_id", max_length=63)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project_id):
        raise ValueError("manifest project_id is invalid")
    _require_string(manifest.get("ingestion_profile"), "ingestion_profile", max_length=128)

    collection = _require_string(manifest.get("collection"), "collection", max_length=63)
    if not COLLECTION_PATTERN.fullmatch(collection):
        raise ValueError("manifest collection is invalid")

    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents or len(documents) > 100:
        raise ValueError("manifest documents must contain between 1 and 100 entries")

    seen_ids: set[str] = set()
    seen_filenames: set[str] = set()
    for index, item in enumerate(documents):
        prefix = f"documents[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be a JSON object")

        source_id = _require_string(item.get("source_id"), f"{prefix}.source_id", max_length=64)
        if not SOURCE_ID_PATTERN.fullmatch(source_id) or source_id in seen_ids:
            raise ValueError(f"{prefix}.source_id is invalid or duplicated")
        seen_ids.add(source_id)

        filename = _require_string(item.get("filename"), f"{prefix}.filename", max_length=255)
        if Path(filename).name != filename or filename in {".", ".."} or not filename.lower().endswith(".md"):
            raise ValueError(f"{prefix}.filename must be a Markdown basename without path components")
        if filename in seen_filenames:
            raise ValueError(f"{prefix}.filename is duplicated")
        seen_filenames.add(filename)

        _require_string(item.get("title"), f"{prefix}.title", max_length=256)
        for field in ("knowledge_domain", "knowledge_type"):
            value = _require_string(item.get(field), f"{prefix}.{field}", max_length=64)
            if not SLUG_PATTERN.fullmatch(value):
                raise ValueError(f"{prefix}.{field} is invalid")

        if not SHA256_PATTERN.fullmatch(str(item.get("sha256", ""))):
            raise ValueError(f"{prefix}.sha256 must be a lowercase SHA-256 digest")

        source_version = item.get("source_version")
        if source_version is not None and (
            not isinstance(source_version, str) or len(source_version) > 64
        ):
            raise ValueError(f"{prefix}.source_version must be null or a string of at most 64 characters")

        source_status = item.get("source_status")
        if source_status is not None and (
            not isinstance(source_status, str) or len(source_status) > 128
        ):
            raise ValueError(f"{prefix}.source_status must be null or a bounded string")

        source_status_class = item.get("source_status_class")
        if source_status_class not in SOURCE_STATUS_CLASSES:
            raise ValueError(f"{prefix}.source_status_class is invalid")
        if item.get("verification_status") not in VERIFICATION_STATUSES:
            raise ValueError(f"{prefix}.verification_status is invalid")
        if item.get("implementation_status") not in IMPLEMENTATION_STATUSES:
            raise ValueError(f"{prefix}.implementation_status is invalid")

        source_date = item.get("source_date")
        if source_date is not None:
            if not isinstance(source_date, str):
                raise ValueError(f"{prefix}.source_date must be null or an ISO date")
            try:
                date.fromisoformat(source_date)
            except ValueError as exc:
                raise ValueError(f"{prefix}.source_date must be an ISO date") from exc

        for field in ("agent_ids", "agent_names"):
            values = item.get(field)
            if (
                not isinstance(values, list)
                or len(values) > 100
                or any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"{prefix}.{field} must be a unique array of bounded strings")

    return manifest


def prepare_seed_documents(
    manifest_path: str | Path,
    source_dir: str | Path,
) -> tuple[dict[str, Any], list[PreparedSeedDocument]]:
    """Verify exact source bytes, parse them, and prepare metadata without external I/O."""
    manifest = load_manifest(manifest_path)
    root = Path(source_dir)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source directory must be a real directory, not a symlink")
    root = root.resolve(strict=True)

    prepared: list[PreparedSeedDocument] = []
    for item in manifest["documents"]:
        path = root / item["filename"]
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"source '{item['source_id']}' must be a regular file, not a symlink")
        resolved = path.resolve(strict=True)
        if resolved.parent != root:
            raise ValueError(f"source '{item['source_id']}' escapes the configured source directory")
        if resolved.stat().st_size < 1 or resolved.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError(f"source '{item['source_id']}' must be between 1 byte and 10 MiB")

        raw = resolved.read_bytes()
        source_sha256 = hashlib.sha256(raw).hexdigest()
        if source_sha256 != item["sha256"]:
            raise ValueError(
                f"SHA-256 mismatch for source '{item['source_id']}'; "
                "use the exact original document or update the manifest through review"
            )

        source_uri = f"rusa://knowledge-source/{item['source_id']}"
        parsed = parse_file(resolved, source_uri=source_uri)
        normalized = normalize_text(parsed.content)
        reject_credential_material(normalized)

        metadata: dict[str, Any] = {
            "project_id": manifest["project_id"],
            "ingestion_profile": manifest["ingestion_profile"],
            "source_id": item["source_id"],
            "source_filename": item["filename"],
            "source_sha256": source_sha256,
            "source_version": item["source_version"],
            "source_date": item["source_date"],
            "source_status": item["source_status"],
            "source_status_class": item["source_status_class"],
            "verification_status": item["verification_status"],
            "implementation_status": item["implementation_status"],
            "knowledge_domain": item["knowledge_domain"],
            "knowledge_type": item["knowledge_type"],
            "agent_ids": item["agent_ids"],
            "agent_names": item["agent_names"],
            "parser": parsed.metadata["parser"],
            "media_type": parsed.metadata["media_type"],
            "file_size_bytes": int(parsed.metadata["file_size_bytes"]),
            "chunking_strategy": "character_overlap_v1",
            "chunk_size_chars": DEFAULT_CHUNK_CHARS,
            "chunk_overlap_chars": DEFAULT_OVERLAP_CHARS,
        }
        validate_document_metadata(source_uri, item["title"], metadata)
        chunks = chunk_text(normalized)
        if not chunks:
            raise ValueError(f"source '{item['source_id']}' did not produce searchable chunks")

        prepared.append(
            PreparedSeedDocument(
                source_id=item["source_id"],
                title=item["title"],
                source_uri=source_uri,
                content=normalized,
                metadata=metadata,
                source_sha256=source_sha256,
                chunk_count=len(chunks),
            )
        )

    return manifest, prepared


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rusa_knowledge.seed",
        description="Validate or explicitly ingest the pinned RUSA architecture/agent seed corpus.",
    )
    parser.add_argument(
        "--manifest",
        default="docs/knowledge-seed/manifest.json",
        help="source manifest path (default: docs/knowledge-seed/manifest.json)",
    )
    parser.add_argument(
        "--source-dir",
        default=None,
        help="directory containing the original source files (default: <manifest-dir>/sources)",
    )
    parser.add_argument("--tenant-id", help="existing tenant authorized to hold RUSA platform knowledge")
    parser.add_argument("--collection", help="override the manifest collection (default: agent)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write embeddings and documents to PostgreSQL; without this flag, run a non-writing preflight",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest_path = Path(args.manifest)
    source_dir = Path(args.source_dir) if args.source_dir else manifest_path.parent / "sources"
    service = None

    try:
        manifest, documents = prepare_seed_documents(manifest_path, source_dir)
        collection = args.collection or manifest["collection"]
        if not isinstance(collection, str) or not COLLECTION_PATTERN.fullmatch(collection):
            raise ValueError("collection is invalid")

        if not args.apply:
            print(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "project_id": manifest["project_id"],
                        "collection": collection,
                        "document_count": len(documents),
                        "chunk_count": sum(item.chunk_count for item in documents),
                        "documents": [
                            {
                                "source_id": item.source_id,
                                "title": item.title,
                                "source_uri": item.source_uri,
                                "source_sha256": item.source_sha256,
                                "chunk_count": item.chunk_count,
                                "metadata": item.metadata,
                            }
                            for item in documents
                        ],
                        "database_write": False,
                        "embedding_calls": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
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
            results.append(
                {
                    "source_id": item.source_id,
                    "document_id": result["document_id"],
                    "source_uri": result["source_uri"],
                    "chunk_count": result["chunk_count"],
                    "content_hash": result["content_hash"],
                }
            )

        print(
            json.dumps(
                {
                    "mode": "applied",
                    "project_id": manifest["project_id"],
                    "tenant_id": args.tenant_id,
                    "collection": collection,
                    "document_count": len(results),
                    "chunk_count": sum(item["chunk_count"] for item in results),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"RUSA knowledge seed failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
