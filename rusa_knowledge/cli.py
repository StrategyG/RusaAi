"""Operator CLI for local document ingestion and knowledge-store readiness."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .evaluation import evaluate_retrieval
from .parsers import parse_file
from .service import KnowledgeService
from .store import PostgresKnowledgeStore


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

    candidate = commands.add_parser(
        "get-review-candidate",
        help="read an unpublished version through the reviewer-only path",
    )
    candidate.add_argument("--tenant-id", required=True)
    candidate.add_argument("--collection", default="agent")
    candidate.add_argument("--document-id", required=True)
    candidate.add_argument("--version-id", required=True)
    candidate.add_argument("--agent-id")
    candidate.add_argument("--project-id")
    candidate.add_argument("--max-chars", type=int, default=20_000)

    rights = commands.add_parser("attest-rights", help="record a source rights attestation")
    rights.add_argument("--tenant-id", required=True)
    rights.add_argument("--document-id", required=True)
    rights.add_argument("--version-id", required=True)
    rights.add_argument("--rights-status", choices=("unknown", "cleared", "restricted", "denied"), required=True)
    rights.add_argument("--source-owner", required=True)
    rights.add_argument("--license-id", required=True)
    rights.add_argument("--internal-storage-allowed", action="store_true")
    rights.add_argument("--extraction-allowed", action="store_true")
    rights.add_argument("--derivative-allowed", action="store_true")
    rights.add_argument("--embedding-allowed", action="store_true")
    rights.add_argument("--retrieval-allowed", action="store_true")
    rights.add_argument("--attribution-required", action="store_true")
    rights.add_argument("--attribution-text")
    rights.add_argument("--retention-policy", required=True)
    rights.add_argument("--evidence-uri", required=True)

    review = commands.add_parser("review-version", help="record a technical/source/security/QA review")
    review.add_argument("--tenant-id", required=True)
    review.add_argument("--document-id", required=True)
    review.add_argument("--version-id", required=True)
    review.add_argument("--review-type", choices=("technical", "source_fidelity", "security", "qa"), required=True)
    review.add_argument("--decision", choices=("passed", "needs_changes", "failed"), required=True)
    review.add_argument("--finding", required=True)
    review.add_argument("--evidence-uri")

    approval = commands.add_parser("approve-version", help="record a separate human approval decision")
    approval.add_argument("--tenant-id", required=True)
    approval.add_argument("--document-id", required=True)
    approval.add_argument("--version-id", required=True)
    approval.add_argument("--decision", choices=("approved", "rejected", "revoked"), required=True)
    approval.add_argument("--approval-reference", required=True)

    publication = commands.add_parser("publish-version", help="publish only after rights, review and approval gates pass")
    publication.add_argument("--tenant-id", required=True)
    publication.add_argument("--document-id", required=True)
    publication.add_argument("--version-id", required=True)
    publication.add_argument("--manifest-uri", required=True)
    publication.add_argument("--manifest-sha256", required=True)

    withdrawal = commands.add_parser("withdraw-version", help="withdraw a published version from answer-facing retrieval")
    withdrawal.add_argument("--tenant-id", required=True)
    withdrawal.add_argument("--document-id", required=True)
    withdrawal.add_argument("--version-id", required=True)
    withdrawal.add_argument("--reason", required=True)

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
    governance_store = None
    try:
        if args.command in {
            "get-review-candidate", "attest-rights", "review-version",
            "approve-version", "publish-version", "withdraw-version",
        }:
            expected_governance_role = {
                "get-review-candidate": "rusa_knowledge_reviewer",
                "attest-rights": "rusa_knowledge_reviewer",
                "review-version": "rusa_knowledge_reviewer",
                "approve-version": "rusa_knowledge_approver",
                "publish-version": "rusa_knowledge_publisher",
                "withdraw-version": "rusa_knowledge_publisher",
            }[args.command]
            if os.getenv("RUSA_KNOWLEDGE_DB_ROLE") != expected_governance_role:
                raise PermissionError(
                    f"{args.command} requires RUSA_KNOWLEDGE_DB_ROLE={expected_governance_role}"
                )
            dsn = os.getenv("RUSA_KNOWLEDGE_DATABASE_URL") or os.getenv("RUSA_AGENT_DATABASE_URL")
            if not dsn:
                raise RuntimeError("RUSA_KNOWLEDGE_DATABASE_URL is required")
            dimensions = int(os.getenv("RUSA_EMBEDDING_DIMENSIONS", "1536"))
            governance_store = PostgresKnowledgeStore(
                dsn,
                dimensions=dimensions,
                db_role=os.getenv("RUSA_KNOWLEDGE_DB_ROLE") or None,
                require_publication=True,
            )
            if not governance_store.check_ready():
                raise RuntimeError("knowledge governance schema is not ready")
            if args.command == "get-review-candidate":
                if os.getenv("RUSA_KNOWLEDGE_DB_ROLE") != "rusa_knowledge_reviewer":
                    raise PermissionError("get-review-candidate requires RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_reviewer")
                result = governance_store.get_review_candidate_version(
                    tenant_id=args.tenant_id,
                    collection=args.collection,
                    document_id=args.document_id,
                    version_id=args.version_id,
                    agent_id=args.agent_id,
                    project_id=args.project_id,
                    max_chars=args.max_chars,
                )
                print(json.dumps({"found": result is not None, "version": result}, ensure_ascii=False))
                return 0
            if args.command == "attest-rights":
                result = governance_store.record_rights_attestation(
                    tenant_id=args.tenant_id,
                    document_id=args.document_id,
                    version_id=args.version_id,
                    rights_status=args.rights_status,
                    source_owner=args.source_owner,
                    license_id=args.license_id,
                    internal_storage_allowed=args.internal_storage_allowed,
                    extraction_allowed=args.extraction_allowed,
                    derivative_allowed=args.derivative_allowed,
                    embedding_allowed=args.embedding_allowed,
                    retrieval_allowed=args.retrieval_allowed,
                    attribution_required=args.attribution_required,
                    attribution_text=args.attribution_text,
                    retention_policy=args.retention_policy,
                    evidence_uri=args.evidence_uri,
                )
            elif args.command == "review-version":
                result = governance_store.record_review_finding(
                    tenant_id=args.tenant_id,
                    document_id=args.document_id,
                    version_id=args.version_id,
                    review_type=args.review_type,
                    decision=args.decision,
                    finding_text=args.finding,
                    evidence_uri=args.evidence_uri,
                )
            elif args.command == "approve-version":
                result = governance_store.record_human_approval(
                    tenant_id=args.tenant_id,
                    document_id=args.document_id,
                    version_id=args.version_id,
                    decision=args.decision,
                    approval_reference=args.approval_reference,
                )
            elif args.command == "publish-version":
                result = governance_store.publish_version(
                    tenant_id=args.tenant_id,
                    document_id=args.document_id,
                    version_id=args.version_id,
                    manifest_uri=args.manifest_uri,
                    manifest_sha256=args.manifest_sha256,
                )
            else:
                result = governance_store.withdraw_version(
                    tenant_id=args.tenant_id,
                    document_id=args.document_id,
                    version_id=args.version_id,
                    reason=args.reason,
                )
            print(json.dumps({"event_id": result}, ensure_ascii=False))
            return 0

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
        if governance_store is not None:
            governance_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
