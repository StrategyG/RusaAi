"""Offline retrieval metrics for tenant-separated RAG evaluation datasets."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import Any

from rusa_observability.metrics import record_retrieval_evaluation


def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if k < 1:
        raise ValueError("k must be positive")
    if not relevant_ids:
        raise ValueError("relevant document set must not be empty")
    return len(set(retrieved_ids[:k]) & relevant_ids) / len(relevant_ids)


def reciprocal_rank_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if k < 1:
        raise ValueError("k must be positive")
    if not relevant_ids:
        raise ValueError("relevant document set must not be empty")
    for rank, document_id in enumerate(retrieved_ids[:k], start=1):
        if document_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if k < 1:
        raise ValueError("k must be positive")
    if not relevant_ids:
        raise ValueError("relevant document set must not be empty")
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, document_id in enumerate(retrieved_ids[:k], start=1)
        if document_id in relevant_ids
    )
    ideal_count = min(len(relevant_ids), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return dcg / idcg if idcg else 0.0


def evaluate_retrieval(
    cases: Iterable[dict[str, Any]],
    retrieve: Callable[[str, int], list[dict[str, Any]]],
    *,
    k: int = 5,
    max_cases: int = 10_000,
) -> dict[str, Any]:
    """Evaluate ranked document IDs against a manually curated relevance dataset."""
    if not 1 <= k <= 20 or not 1 <= max_cases <= 100_000:
        raise ValueError("evaluation limits are invalid")

    recall_values: list[float] = []
    reciprocal_values: list[float] = []
    ndcg_values: list[float] = []
    count = 0
    for case in cases:
        count += 1
        if count > max_cases:
            raise ValueError("evaluation dataset exceeds the configured case limit")
        query = case.get("query")
        relevant = case.get("relevant_document_ids")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("each evaluation case needs a bounded non-empty query")
        if (
            not isinstance(relevant, list)
            or not relevant
            or any(not isinstance(item, str) or not item for item in relevant)
        ):
            raise ValueError("each evaluation case needs relevant_document_ids")
        relevant_ids = set(relevant)
        results = retrieve(query, k)
        if not isinstance(results, list):
            raise ValueError("retriever must return a list of ranked documents")
        retrieved_ids = list(dict.fromkeys(
            item["document_id"]
            for item in results
            if isinstance(item, dict) and isinstance(item.get("document_id"), str)
        ))[:k]
        recall_values.append(recall_at_k(retrieved_ids, relevant_ids, k))
        reciprocal_values.append(reciprocal_rank_at_k(retrieved_ids, relevant_ids, k))
        ndcg_values.append(ndcg_at_k(retrieved_ids, relevant_ids, k))

    if count == 0:
        raise ValueError("evaluation dataset must contain at least one case")
    report = {
        "queries": count,
        "k": k,
        "recall_at_k": sum(recall_values) / count,
        "mrr_at_k": sum(reciprocal_values) / count,
        "ndcg_at_k": sum(ndcg_values) / count,
    }
    record_retrieval_evaluation(report)
    return report
