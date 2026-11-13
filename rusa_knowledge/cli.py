"""Operator CLI for local document ingestion and knowledge-store readiness."""

from __future__ import annotations

import argparse
import json
import sys

from .evaluation import evaluate_retrieval
from .parsers import parse_file
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
    ingest.add_argument(
        "--knowledge-scope",
        choices=("shared", "agent_specific"),
        default="shared",
        help="shared knowledge or knowledge restricted to selected AICode Worker roles",
    )
    ingest.add_argument(
        "--agent-id",
        action="append",
        default=[],
        help="target agent ID; repeat for multiple agents (required for agent_specific scope)",
    )
    ingest.add_argument("--project-id", help="optional server-governed project scope for agent_specific knowledge")

    commands.add_parser("check", help="verify PostgreSQL, pgvector, and RLS readiness")

    versions = commands.add_parser("versions", help="list immutable versions of one document")
    versions.add_argument("--tenant-id", required=True)
    versions.add_argument("--collection", default="agent")
    versions.add_argument("--document-id", required=True)
    versions.add_argument("--agent-id")
    versions.add_argument("--project-id")
    versions.add_argument("--limit", type=int, default=20)

    get_version = commands.add_parser("get-version", help="read one immutable document version")
    get_version.add_argument("--tenant-id", required=True)
    get_version.add_argument("--collection", default="agent")
    get_version.add_argument("--document-id", required=True)
    get_version.add_argument("--version-id", required=True)
    get_version.add_argument("--agent-id")
    get_version.add_argument("--project-id")
    get_version.add_argument("--max-chars", type=int, default=20_000)

    evaluation = commands.add_parser("evaluate", help="evaluate retrieval against a JSONL relevance dataset")
    evaluation.add_argument("--dataset", required=True)
    evaluation.add_argument("--tenant-id", required=True)
    evaluation.add_argument("--collection", default="agent")
    evaluation.add_argument("--agent-id", help="agent filter for scoped retrieval evaluation")
    evaluation.add_argument("--project-id", help="project filter for scoped retrieval evaluation")
    evaluation.add_argument("--k", type=int, default=5)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    service = None
    try:
        service = KnowledgeService.from_env(check_ready=True)
        if args.command == "check":
            print(json.dumps({"ready": service.store.check_ready()}))
            return 0

        if args.command == "versions":
            versions = service.list_document_versions(
                tenant_id=args.tenant_id,
                collection=args.collection,
                document_id=args.document_id,
                agent_id=args.agent_id,
                project_id=args.project_id,
                limit=args.limit,
            )
            print(json.dumps({"versions": versions}, ensure_ascii=False))
            return 0

        if args.command == "get-version":
            version = service.get_document_version(
                tenant_id=args.tenant_id,
                collection=args.collection,
                document_id=args.document_id,
                version_id=args.version_id,
                agent_id=args.agent_id,
                project_id=args.project_id,
                max_chars=args.max_chars,
            )
            print(json.dumps({"found": version is not None, "version": version}, ensure_ascii=False))
            return 0

        if args.command == "evaluate":
            from pathlib import Path

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
                    agent_id=args.agent_id,
                    project_id=args.project_id,
                ),
                k=args.k,
            )
            report["scope"] = {
                "tenant_id": args.tenant_id,
                "collection": args.collection,
                "agent_id": args.agent_id,
                "project_id": args.project_id,
            }
            print(json.dumps(report, ensure_ascii=False))
            return 0

        parsed = parse_file(args.file, source_uri=args.source_uri)
        metadata = dict(parsed.metadata)
        metadata["knowledge_scope"] = args.knowledge_scope
        if args.agent_id:
            metadata["agent_ids"] = args.agent_id
        if args.project_id:
            metadata["project_id"] = args.project_id
        result = service.ingest_text(
            tenant_id=args.tenant_id,
            collection=args.collection,
            source_uri=parsed.source_uri,
            title=args.title or parsed.title,
            content=parsed.content,
            metadata=metadata,
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
