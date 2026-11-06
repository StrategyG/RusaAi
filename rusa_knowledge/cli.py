"""Operator CLI for local document ingestion and knowledge-store readiness."""

from __future__ import annotations

import argparse
import json
import sys

from .parsers import parse_file
from .service import KnowledgeService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rusa-knowledge")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="ingest a local document into a tenant collection")
    ingest.add_argument("--file", required=True)
    ingest.add_argument("--tenant-id", required=True)
    ingest.add_argument("--collection", default="default")
    ingest.add_argument("--source-uri")
    ingest.add_argument("--title")

    commands.add_parser("check", help="verify PostgreSQL, pgvector, and RLS readiness")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    service = None
    try:
        service = KnowledgeService.from_env(check_ready=True)
        if args.command == "check":
            print(json.dumps({"ready": service.store.check_ready()}))
            return 0

        parsed = parse_file(args.file, source_uri=args.source_uri)
        result = service.ingest_text(
            tenant_id=args.tenant_id,
            collection=args.collection,
            source_uri=parsed.source_uri,
            title=args.title or parsed.title,
            content=parsed.content,
            metadata=parsed.metadata,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"knowledge operation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
