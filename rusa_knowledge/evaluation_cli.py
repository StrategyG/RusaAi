from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

from .evaluation import evaluate_retrieval
from .service import KnowledgeService


def _load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("evaluation dataset must be a file no larger than 20 MiB")
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            if len(cases) >= 10_000:
                raise ValueError("evaluation dataset exceeds 10000 cases")
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on evaluation line {line_number}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"evaluation line {line_number} must contain an object")
            cases.append(item)
    if not cases:
        raise ValueError("evaluation dataset is empty")
    return cases


def _push_report(report: dict[str, Any], endpoint: str) -> None:
    registry = CollectorRegistry()
    k = str(report["k"])
    Gauge(
        "rusa_rag_eval_recall_at_k",
        "Mean Recall@k from the latest curated retrieval evaluation.",
        ["k"],
        registry=registry,
    ).labels(k).set(float(report["recall_at_k"]))
    Gauge(
        "rusa_rag_eval_mrr_at_k",
        "Mean reciprocal rank from the latest curated retrieval evaluation.",
        ["k"],
        registry=registry,
    ).labels(k).set(float(report["mrr_at_k"]))
    Gauge(
        "rusa_rag_eval_ndcg_at_k",
        "Mean NDCG@k from the latest curated retrieval evaluation.",
        ["k"],
        registry=registry,
    ).labels(k).set(float(report["ndcg_at_k"]))
    Gauge(
        "rusa_rag_eval_last_run_timestamp_seconds",
        "Unix timestamp of the latest curated retrieval evaluation.",
        registry=registry,
    ).set(time.time())
    Gauge(
        "rusa_rag_eval_queries",
        "Number of cases in the latest curated retrieval evaluation.",
        registry=registry,
    ).set(int(report["queries"]))
    push_to_gateway(endpoint, job="rusa-rag-evaluation", registry=registry)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate tenant-scoped RUSA-AI retrieval quality.")
    parser.add_argument(
        "--dataset",
        default=os.getenv("RUSA_RAG_EVAL_DATASET", "/var/lib/rusa-eval/dataset.jsonl"),
    )
    parser.add_argument("--tenant-id", default=os.getenv("RUSA_RAG_EVAL_TENANT_ID", ""))
    parser.add_argument("--collection", default=os.getenv("RUSA_KNOWLEDGE_COLLECTION", "agent"))
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    if not args.tenant_id:
        parser.error("RUSA_RAG_EVAL_TENANT_ID or --tenant-id is required")

    service = KnowledgeService.from_env(check_ready=True)
    try:
        cases = _load_cases(Path(args.dataset))
        report = evaluate_retrieval(
            cases,
            lambda query, limit: service.search(
                tenant_id=args.tenant_id,
                collection=args.collection,
                query=query,
                limit=limit,
            ),
            k=args.k,
            max_cases=10_000,
        )
    finally:
        service.close()

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    push_endpoint = os.getenv("RUSA_PROMETHEUS_PUSHGATEWAY", "").strip()
    if push_endpoint:
        _push_report(report, push_endpoint)

    minimum_recall = float(os.getenv("RUSA_RAG_MIN_RECALL_AT_K", "0.80"))
    if not 0.0 <= minimum_recall <= 1.0:
        raise ValueError("RUSA_RAG_MIN_RECALL_AT_K must be between 0 and 1")
    return 0 if report["recall_at_k"] >= minimum_recall else 2


if __name__ == "__main__":
    sys.exit(main())
