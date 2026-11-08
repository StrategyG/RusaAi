import pytest

from rusa_agents.benchmarking import (
    BENCHMARK_WEIGHTS,
    aggregate_benchmark_scores,
    normalize_answer,
    score_reasoning_cases,
)


def test_normalize_answer_handles_case_punctuation_and_unicode():
    assert normalize_answer("  JAWABAN: Empat Puluh Dua! ") == "jawaban empat puluh dua"
    assert normalize_answer("ＡＢＣ") == "abc"


def test_reasoning_score_reports_overall_and_per_category_accuracy():
    cases = [
        {"id": "r1", "category": "arithmetic", "accepted_answers": ["42", "empat puluh dua"]},
        {"id": "r2", "category": "logic", "accepted_answers": ["tidak"]},
        {"id": "r3", "category": "logic", "accepted_answers": ["ya"]},
    ]
    report = score_reasoning_cases(
        cases,
        {"r1": "42.", "r2": "Tidak!", "r3": "mungkin"},
    )

    assert report["total"] == 3
    assert report["passed"] == 2
    assert report["failed"] == 1
    assert report["accuracy"] == pytest.approx(2 / 3)
    assert report["category_scores"]["logic"]["accuracy"] == 0.5


def test_reasoning_score_rejects_empty_or_duplicate_dataset():
    with pytest.raises(ValueError, match="at least one"):
        score_reasoning_cases([], {})
    duplicate = [
        {"id": "same", "category": "logic", "accepted_answers": ["yes"]},
        {"id": "same", "category": "logic", "accepted_answers": ["no"]},
    ]
    with pytest.raises(ValueError, match="unique"):
        score_reasoning_cases(duplicate, {})


def test_reasoning_score_rejects_invalid_accepted_answers():
    cases = [{"id": "r1", "category": "logic", "accepted_answers": []}]
    with pytest.raises(ValueError, match="accepted_answers"):
        score_reasoning_cases(cases, {})


def test_weighted_score_requires_all_seven_categories_and_applies_security_gate():
    scores = {category: 100 for category in BENCHMARK_WEIGHTS}
    report = aggregate_benchmark_scores(scores)
    assert report["score"] == 100
    assert report["release_eligible"] is True

    scores["security"] = 94
    blocked = aggregate_benchmark_scores(scores)
    assert blocked["release_eligible"] is False
    assert blocked["release_blockers"]


def test_weighted_score_rejects_missing_and_out_of_range_values():
    with pytest.raises(ValueError, match="exactly seven"):
        aggregate_benchmark_scores({"rag": 100})
    scores = {category: 100 for category in BENCHMARK_WEIGHTS}
    scores["security"] = 101
    with pytest.raises(ValueError, match="between 0 and 100"):
        aggregate_benchmark_scores(scores)
