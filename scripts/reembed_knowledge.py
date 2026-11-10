#!/usr/bin/env python3
"""Safely backfill 384-dimensional RusaModels E5 vectors alongside legacy 1536d vectors."""
from __future__ import annotations

import argparse
import json
import sys

from rusa_knowledge.service import KnowledgeService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--collection", help="Optional collection; omit to process all tenant collections")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-chunks", type=int, default=10_000)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write new 384d vectors. Without this flag the command is read-only (dry run).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.batch_size <= 64:
        raise ValueError("--batch-size must be between 1 and 64")
    if not 1 <= args.max_chunks <= 1_000_000:
        raise ValueError("--max-chunks must be between 1 and 1000000")

    service = None
    try:
        service = KnowledgeService.from_env(check_ready=True)
        if service.store.dimensions != 384:
            raise RuntimeError("Set RUSA_EMBEDDING_DIMENSIONS=384 before running this command")
        if not getattr(service.embeddings, "input_type_enabled", False):
            raise RuntimeError("RusaModels E5 query/passage input types must be enabled")

        after_chunk_id = None
        scanned = 0
        updated = 0
        while scanned < args.max_chunks:
            limit = min(args.batch_size, args.max_chunks - scanned)
            batch = service.store.fetch_chunks_for_reembedding(
                tenant_id=args.tenant_id,
                collection=args.collection,
                after_chunk_id=after_chunk_id,
                limit=limit,
            )
            if not batch:
                break
            scanned += len(batch)
            after_chunk_id = batch[-1]["chunk_id"]
            if args.apply:
                vectors = service.embed_passages([item["content"] for item in batch])
                updated += service.store.update_reembedded_embeddings(
                    tenant_id=args.tenant_id,
                    vectors=[
                        (item["chunk_id"], vector)
                        for item, vector in zip(batch, vectors, strict=True)
                    ],
                )

        print(
            json.dumps(
                {
                    "tenant_id": args.tenant_id,
                    "collection": args.collection,
                    "scanned": scanned,
                    "updated": updated,
                    "mode": "apply" if args.apply else "dry_run",
                    "max_chunks": args.max_chunks,
                    "note": (
                        "Legacy 1536d vectors were retained."
                        if args.apply
                        else "No database rows were modified. Re-run with --apply to write vectors."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(f"knowledge re-embedding failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
