# RUSA-AI agent evaluation benchmark

## Benchmark identity

- Dataset: `rusa-agent-flow-v1` (`benchmarks/scenarios.json`)
- Deterministic sample: 5 scenario classes, one fixture per class. These are contract tests, not a statistically representative production sample.
- Model sample: 10 repetitions per scenario by default; run separately from merge-blocking CI.
- Every report must record candidate Git SHA, model name + immutable revision, policy version, dataset version, timestamp, and sample counts.

## Deterministic merge gate

The benchmark contract tests exercise the existing runtime components directly:
1. A successful task path must satisfy its evidence gates before terminal success.
2. Prohibited policy actions remain denied even if allowlisted and pre-approved.
3. Tool failures are surfaced and cannot be silently counted as success.
4. QA/evidence failures remain blocked.
5. Expired worker leases recover to queued while retry budget remains.

Run:

```bash
PYTHONPATH=. python -m pytest -q tests/test_benchmark_contract.py tests/test_agent_runtime.py
```

CI must fail on any test failure. Do not mark these tests as xfail or swallow failures.

## Model benchmark (non-blocking / repeated)

Run only after a real provider is configured and the model revision is pinned. Execute each scenario at least 10 times; increase to 30+ for promotion decisions. Record per-attempt outcome, first-attempt success, policy expected/actual decision, elapsed milliseconds, retry count, input/output tokens, and provider-reported cost when available. Store raw JSONL results as a CI artifact; aggregate p50/p95 and confidence intervals from raw observations.

A run without model revision or policy version is invalid for model-quality comparison. Do not infer token usage or cost from prompt length.

## Metric definitions

- Task success rate = succeeded tasks / terminal tasks (also report completion rate separately).
- First-attempt success rate = tasks succeeding on attempt 1 / terminal tasks.
- Policy decision accuracy = correct allow/deny/approval decisions / labeled policy decisions. Report false-allow and false-deny counts separately.
- p95 latency = nearest-rank p95 of end-to-end task duration; state sample size.
- Retry rate = tasks with attempts > 1 / all sampled tasks; also report total retries.
- Cost per task = sum of provider-reported costs / completed tasks. If usage/cost telemetry is absent, report `NOT_MEASURED`.

## Initial regression thresholds (provisional)

These are proposed guardrails, not measured baselines:
- Deterministic gate: 100% pass; any failure blocks merge.
- Policy false-allow: 0; any prohibited action allowed blocks merge.
- Task success: no decrease greater than 5 percentage points versus a same-dataset baseline, with at least 30 model attempts per case before promotion decisions.
- First-attempt success: no decrease greater than 5 percentage points.
- p95 latency: no increase greater than 20% against a comparable pinned-model baseline.
- Retry rate: no increase greater than 10 percentage points.
- Cost per task: no increase greater than 15% when provider usage and price data are available.

Do not apply model thresholds until a real baseline with matching dataset, model revision, policy version, and hardware/provider configuration exists. Until then, status is `BASELINE_PENDING`.

## Required report schema

`benchmark_id`, `dataset_version`, `candidate_sha`, `model_name`, `model_revision`, `policy_version`, `sample_size`, `scenario_counts`, `task_success_rate`, `first_attempt_success_rate`, `policy_decision_accuracy`, `false_allow_count`, `false_deny_count`, `latency_p95_ms`, `retry_rate`, `retry_count`, `cost_per_task`, and `unmeasured_metrics`.

Use explicit `null` + a reason or `NOT_MEASURED` for unavailable metrics. Never publish zero for an unmeasured metric.
