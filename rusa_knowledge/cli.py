"""Operator CLI for local documents, large Parquet telemetry, and RAG evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote

from .evaluation import evaluate_retrieval
from .parsers import parse_file
from .parquet import iter_telemetry_aggregates
from .service import KnowledgeService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rusa-knowledge")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="ingest a local document into a tenant collection")
    ingest.add_argument("--file", required=True)
    ingest.add_argument("--tenant-id", required=True)
    ingest.add_argument("--collection", default="agent")
    ingest.add_argument("--source-uri")
    ingest.add_argument("--title")

    parquet = commands.add_parser(
        "ingest-parquet",
        help="aggregate a large Parquet telemetry file out-of-core and index its summaries",
    )
    parquet.add_argument("--file", required=True)
    parquet.add_argument("--tenant-id", required=True)
    parquet.add_argument("--collection", default="devops")
    parquet.add_argument("--timestamp-column", required=True)
    parquet.add_argument("--group-column", required=True)
    parquet.add_argument("--metric-columns", required=True, help="comma-separated numeric columns")
    parquet.add_argument("--bucket-minutes", type=int, default=60)
    parquet.add_argument("--rows-per-document", type=int, default=100)
    parquet.add_argument("--max-documents", type=int, default=10000)
    parquet.add_argument("--batch-size", type=int, default=1024)
    parquet.add_argument("--memory-limit", default="2GB")
    parquet.add_argument("--threads", type=int, default=4)
    parquet.add_argument("--temp-directory")
    parquet.add_argument("--dry-run", action="store_true")

    commands.add_parser("check", help="verify PostgreSQL, pgvector, RLS, and embedding readiness")

    evaluation = commands.add_parser("evaluate", help="evaluate retrieval against a JSONL relevance dataset")
    evaluation.add_argument("--dataset", required=True)
    evaluation.add_argument("--tenant-id", required=True)
    evaluation.add_argument("--collection", default="default")
    evaluation.add_argument("--k", type=int, default=5)
    return parser


def _parquet_options(args: argparse.Namespace) -> dict:
    path = Path(args.file)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Parquet input must be a regular local file, not a symlink")
    stat = path.stat()
    fingerprint = hashlib.sha256(
        f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:20]
    metrics = [item.strip() for item in args.metric_columns.split(",") if item.strip()]
    if not 1 <= args.rows_per_document <= 500:
        raise ValueError("rows-per-document must be between 1 and 500")
    if not 1 <= args.max_documents <= 10000:
        raise ValueError("max-documents must be between 1 and 10000")
    return {
        "path": path,
        "fingerprint": fingerprint,
        "metrics": metrics,
        "max_aggregates": args.rows_per_document * args.max_documents,
    }


def _iter_parquet(args: argparse.Namespace, options: dict):
    return iter_telemetry_aggregates(
        options["path"],
        timestamp_column=args.timestamp_column,
        group_column=args.group_column,
        metric_columns=options["metrics"],
        bucket_minutes=args.bucket_minutes,
        batch_size=args.batch_size,
        memory_limit=args.memory_limit,
        threads=args.threads,
        temp_directory=args.temp_directory,
        max_aggregates=options["max_aggregates"],
    )


def _ingest_parquet(args: argparse.Namespace, service: KnowledgeService, options: dict) -> dict:
    if not options["metrics"]:
        raise ValueError("at least one metric column is required")
    if not 1 <= args.batch_size <= 10000:
        raise ValueError("batch-size must be between 1 and 10000")

    pending: list[dict] = []
    document_count = 0
    aggregate_count = 0
    raw_row_count = 0
    source_name = quote(options["path"].name, safe="")
    fingerprint = options["fingerprint"]

    def flush() -> None:
        nonlocal document_count, raw_row_count
        if not pending:
            return
        part = document_count
        content = "\n".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in pending
        )
        source_uri = f"parquet://{fingerprint}/{source_name}?part={part:08d}"
        first_bucket = pending[0]["bucket_start"]
        last_bucket = pending[-1]["bucket_start"]
        source_rows = sum(item["row_count"] for item in pending)
        result = service.ingest_text(
            tenant_id=args.tenant_id,
            collection=args.collection,
            source_uri=source_uri,
            title=f"{options['path'].stem} telemetry {part + 1:06d}",
            content=content,
            metadata={
                "ingestion_format": "parquet_aggregated",
                "source_file": options["path"].name,
                "source_fingerprint": fingerprint,
                "timestamp_column": args.timestamp_column,
                "group_column": args.group_column,
                "metric_columns": options["metrics"],
                "bucket_minutes": args.bucket_minutes,
                "aggregate_rows": len(pending),
                "raw_rows_aggregated": source_rows,
                "first_bucket": first_bucket,
                "last_bucket": last_bucket,
                "part": part,
            },
        )
        document_count += 1
        raw_row_count += source_rows
        print(json.dumps(result, ensure_ascii=False))
        pending.clear()

    for aggregate in _iter_parquet(args, options):
        aggregate_count += 1
        if aggregate_count > options["max_aggregates"]:
            raise ValueError(
                "Parquet aggregation exceeded the configured document limit; "
                "increase --max-documents after reviewing expected index size"
            )
        pending.append(aggregate)
        if len(pending) >= args.rows_per_document:
            flush()
    flush()
    return {
        "documents_ingested": document_count,
        "aggregate_rows": aggregate_count,
        "raw_rows_aggregated": raw_row_count,
        "source_fingerprint": fingerprint,
    }


def _dry_run_parquet(args: argparse.Namespace, options: dict) -> int:
    if not options["metrics"]:
        raise ValueError("at least one metric column is required")
    count = 0
    raw_rows = 0
    sample = []
    for aggregate in _iter_parquet(args, options):
        count += 1
        raw_rows += aggregate["row_count"]
        if len(sample) < 3:
            sample.append(aggregate)
        if count > options["max_aggregates"]:
            raise ValueError("Parquet aggregation exceeds --max-documents capacity")
    print(json.dumps({
        "dry_run": True,
        "aggregate_rows": count,
        "raw_rows_aggregated": raw_rows,
        "estimated_documents": (count + args.rows_per_document - 1) // args.rows_per_document,
        "sample": sample,
    }, ensure_ascii=False, default=str))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    service = None
    try:
        if args.command == "ingest-parquet":
            options = _parquet_options(args)
            if args.dry_run:
                return _dry_run_parquet(args, options)

        service = KnowledgeService.from_env(check_ready=True)
        if args.command == "check":
            print(json.dumps({"ready": service.check_ready()}))
            return 0

        if args.command == "evaluate":
            dataset_path = Path(args.dataset)
            if dataset_path.is_symlink() or not dataset_path.is_file():
                raise ValueError("evaluation dataset must be a regular local file")
            if dataset_path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("evaluation dataset exceeds 10 MiB")
            cases = []
            for line in dataset_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    case = json.loads(line)
                    if not isinstance(case, dict):
                        raise ValueError("each evaluation JSONL line must be an object")
                    cases.append(case)
                    if len(cases) > 10_000:
                        raise ValueError("evaluation dataset exceeds 10000 cases")
            report = evaluate_retrieval(
                cases,
                lambda query, limit: service.search(
                    tenant_id=args.tenant_id,
                    collection=args.collection,
                    query=query,
                    limit=limit,
                ),
                k=args.k,
            )
            print(json.dumps(report, ensure_ascii=False))
            return 0

        if args.command == "ingest-parquet":
            result = _ingest_parquet(args, service, options)
            print(json.dumps({"status": "complete", **result}, ensure_ascii=False))
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
