"""Deterministic scoring primitives for the RUSA-AI benchmark suite."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

BENCHMARK_WEIGHTS: dict[str, float] = {
    "rag": 0.15,
    "inference": 0.15,
    "reasoning": 0.20,
    "multi_agent": 0.15,
    "autonomy": 0.15,
    "security": 0.10,
    "performance": 0.10,
}


def normalize_answer(value: str) -> str:
    """Normalize a short answer for deterministic exact-match evaluation."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def score_reasoning_cases(
    cases: Iterable[Mapping[str, Any]],
    predictions: Mapping[str, str],
) -> dict[str, Any]:
    """Score reasoning cases using accepted-answer normalized exact match.

    This deliberately does not claim to measure general reasoning quality,
    groundedness, or hallucination. Those require separately curated evaluators.
    """
    materialized = list(cases)
    if not materialized:
        raise ValueError("reasoning dataset must contain at least one case")

    seen_ids: set[str] = set()
    category_totals: dict[str, int] = defaultdict(int)
    category_passed: dict[str, int] = defaultdict(int)
    results: list[dict[str, Any]] = []

    for case in materialized:
        case_id = str(case.get("id", "")).strip()
        category = str(case.get("category", "")).strip()
        accepted = case.get("accepted_answers")
        if not case_id or case_id in seen_ids:
            raise ValueError("every reasoning case must have a unique non-empty id")
        if not category:
            raise ValueError(f"reasoning case {case_id!r} has no category")
        if not isinstance(accepted, list) or not accepted or not all(
            isinstance(answer, str) and answer.strip() for answer in accepted
        ):
            raise ValueError(f"reasoning case {case_id!r} needs accepted_answers")
        seen_ids.add(case_id)

        actual = predictions.get(case_id, "")
        if not isinstance(actual, str):
            actual = ""
        accepted_normalized = {normalize_answer(answer) for answer in accepted}
        actual_normalized = normalize_answer(actual)
        passed = bool(actual_normalized and actual_normalized in accepted_normalized)

        category_totals[category] += 1
        category_passed[category] += int(passed)
        results.append(
            {
                "id": case_id,
                "category": category,
                "passed": passed,
                "actual_answer": actual,
                "accepted_answers": accepted,
                "scoring_method": "normalized_exact_match",
            }
        )

    total = len(results)
    passed_count = sum(int(result["passed"]) for result in results)
    category_scores = {
        category: {
            "passed": category_passed[category],
            "total": count,
            "accuracy": round(category_passed[category] / count, 6),
        }
        for category, count in sorted(category_totals.items())
    }
    return {
        "scoring_method": "normalized_exact_match",
        "total": total,
        "passed": passed_count,
        "failed": total - passed_count,
        "accuracy": round(passed_count / total, 6),
        "category_scores": category_scores,
        "cases": results,
    }


def aggregate_benchmark_scores(scores: Mapping[str, float]) -> dict[str, Any]:
    """Aggregate seven 0..100 category scores and enforce the security hard gate."""
    missing = sorted(set(BENCHMARK_WEIGHTS) - set(scores))
    extra = sorted(set(scores) - set(BENCHMARK_WEIGHTS))
    if missing or extra:
        raise ValueError(f"expected exactly seven benchmark categories; missing={missing}, extra={extra}")

    normalized: dict[str, float] = {}
    for category, score in scores.items():
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(f"score for {category} must be numeric")
        if not 0 <= score <= 100:
            raise ValueError(f"score for {category} must be between 0 and 100")
        normalized[category] = float(score)

    weighted_score = sum(BENCHMARK_WEIGHTS[key] * normalized[key] for key in BENCHMARK_WEIGHTS)
    security_blocked = normalized["security"] < 95.0
    return {
        "score": round(weighted_score, 2),
        "category_scores": normalized,
        "weights": BENCHMARK_WEIGHTS.copy(),
        "release_eligible": not security_blocked,
        "release_blockers": (
            ["security score is below the mandatory 95/100 hard gate"] if security_blocked else []
        ),
    }
