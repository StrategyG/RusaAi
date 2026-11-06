import pytest

from rusa_knowledge.evaluation import (
    evaluate_retrieval,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
)


def test_retrieval_metrics_calculate_expected_values():
    retrieved = ["doc-a", "doc-x", "doc-b"]
    relevant = {"doc-a", "doc-b"}
    assert recall_at_k(retrieved, relevant, 3) == 1.0
    assert reciprocal_rank_at_k(retrieved, relevant, 3) == 1.0
    assert ndcg_at_k(retrieved, relevant, 3) == pytest.approx(
        (1.0 + 1.0 / 2.0) / (1.0 + 1.0 / 1.5849625007)
    )


def test_evaluation_deduplicates_multiple_chunks_from_one_document():
    report = evaluate_retrieval(
        [{"query": "question", "relevant_document_ids": ["doc-a", "doc-b"]}],
        lambda query, limit: [
            {"document_id": "doc-a"},
            {"document_id": "doc-a"},
            {"document_id": "doc-b"},
        ],
        k=2,
    )
    assert report["queries"] == 1
    assert report["recall_at_k"] == 1.0
    assert report["mrr_at_k"] == 1.0


def test_evaluation_rejects_empty_dataset():
    with pytest.raises(ValueError, match="at least one"):
        evaluate_retrieval([], lambda query, limit: [], k=5)
