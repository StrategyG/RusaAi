from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


THRESHOLDS = {
    "task_success_rate_max_drop": 0.05,
    "first_attempt_success_rate_max_drop": 0.05,
    "policy_decision_accuracy_max_drop": 0.05,
    "latency_p95_max_increase": 0.20,
    "retry_rate_max_increase": 0.10,
    "cost_per_task_max_increase": 0.15,
}
IDENTITY_FIELDS = (
    "benchmark_id",
    "dataset_version",
    "model_name",
    "model_revision",
    "policy_version",
)


def compare_reports(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    min_repetitions_per_scenario: int = 30,
) -> dict[str, Any]:
    mismatches = {
        field: {
            "baseline": baseline.get(field),
            "candidate": candidate.get(field),
        }
        for field in IDENTITY_FIELDS
        if baseline.get(field) != candidate.get(field)
    }
    if mismatches:
        return {
            "status": "INVALID_COMPARISON",
            "identity_mismatches": mismatches,
            "thresholds": THRESHOLDS,
            "regressions": [],
            "skipped_metrics": [],
        }

    baseline_counts = baseline.get("scenario_counts") or {}
    candidate_counts = candidate.get("scenario_counts") or {}
    if set(baseline_counts) != set(candidate_counts):
        return {
            "status": "INVALID_COMPARISON",
            "identity_mismatches": {
                "scenario_counts": {
                    "baseline": baseline_counts,
                    "candidate": candidate_counts,
                }
            },
            "thresholds": THRESHOLDS,
            "regressions": [],
            "skipped_metrics": [],
        }

    insufficient = {
        scenario: {
            "baseline": baseline_counts[scenario],
            "candidate": candidate_counts[scenario],
        }
        for scenario in sorted(baseline_counts)
        if (
            baseline_counts[scenario] < min_repetitions_per_scenario
            or candidate_counts[scenario] < min_repetitions_per_scenario
        )
    }
    if insufficient:
        return {
            "status": "INSUFFICIENT_SAMPLE",
            "minimum_repetitions_per_scenario": min_repetitions_per_scenario,
            "insufficient_scenarios": insufficient,
            "thresholds": THRESHOLDS,
            "regressions": [],
            "skipped_metrics": [],
        }

    regressions: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    if candidate.get("false_allow_count") != 0:
        regressions.append({
            "metric": "false_allow_count",
            "baseline": baseline.get("false_allow_count"),
            "candidate": candidate.get("false_allow_count"),
            "threshold": 0,
            "reason": "Any prohibited policy false-allow blocks promotion",
        })

    if (
        baseline.get("policy_decision_sample_size", 0) < 1
        or candidate.get("policy_decision_sample_size", 0) < 1
    ):
        skipped.append({
            "metric": "policy_decision_accuracy",
            "reason": "No independently labeled policy decisions are available",
        })

    comparisons = (
        ("task_success_rate", "max_drop", THRESHOLDS["task_success_rate_max_drop"]),
        ("first_attempt_success_rate", "max_drop", THRESHOLDS["first_attempt_success_rate_max_drop"]),
        ("policy_decision_accuracy", "max_drop", THRESHOLDS["policy_decision_accuracy_max_drop"]),
        ("latency_p95_ms", "max_increase", THRESHOLDS["latency_p95_max_increase"]),
        ("retry_rate", "max_increase", THRESHOLDS["retry_rate_max_increase"]),
        ("cost_per_task", "max_increase", THRESHOLDS["cost_per_task_max_increase"]),
    )

    for metric, direction, threshold in comparisons:
        old = baseline.get(metric)
        new = candidate.get(metric)
        if old is None or new is None:
            skipped.append({
                "metric": metric,
                "reason": "Baseline or candidate metric is explicitly NOT_MEASURED",
            })
            continue
        if direction == "max_drop":
            delta = old - new
            regressed = delta > threshold
            observed = delta
        else:
            if old == 0:
                regressed = new > 0
                observed = None if new == 0 else "baseline_zero_candidate_positive"
            else:
                delta = (new - old) / abs(old)
                regressed = delta > threshold
                observed = delta
        if regressed:
            regressions.append({
                "metric": metric,
                "baseline": old,
                "candidate": new,
                "observed_regression": observed,
                "threshold": threshold,
            })

    return {
        "status": "FAIL" if regressions else "PASS",
        "baseline_candidate_sha": baseline.get("candidate_sha"),
        "candidate_sha": candidate.get("candidate_sha"),
        "model_name": candidate.get("model_name"),
        "model_revision": candidate.get("model_revision"),
        "policy_version": candidate.get("policy_version"),
        "dataset_version": candidate.get("dataset_version"),
        "minimum_repetitions_per_scenario": min_repetitions_per_scenario,
        "baseline_scenario_counts": baseline_counts,
        "candidate_scenario_counts": candidate_counts,
        "thresholds": THRESHOLDS,
        "regressions": regressions,
        "skipped_metrics": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare RUSA-AI model benchmark reports")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("benchmark-comparison.json"))
    parser.add_argument("--min-repetitions-per-scenario", type=int, default=30)
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    result = compare_reports(
        baseline,
        candidate,
        min_repetitions_per_scenario=args.min_repetitions_per_scenario,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if result["status"] in {"FAIL", "INVALID_COMPARISON", "INSUFFICIENT_SAMPLE"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
