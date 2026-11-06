# RUSA-AI agent evaluation benchmark

## Benchmark identity and evidence

- Dataset: `rusa-agent-flow-v1` (`benchmarks/scenarios.json`).
- Deterministic lifecycle sample: 5 scenarios, one executed fixture per class.
- Labeled policy sample: 5 expected allow/deny/approval decisions.
- CI report: `benchmark-report.json`, uploaded as a workflow artifact.
- Every report records candidate SHA, dataset version, model/provider identity, policy version, sample sizes, and unmeasured metrics.

The deterministic provider is a test double. Its task success rate and in-process SQLite latency are correctness diagnostics, **not production model-quality or production SLO baselines**.

## Deterministic merge gate

Run locally:

```bash
python -m pip install -r requirements-agent.txt pytest pytest-asyncio ruff
ruff check rusa_agents tests/test_benchmark_contract.py tests/test_agent_runtime.py benchmarks/run_deterministic.py
PYTHONPATH=. python -m pytest -q tests/test_benchmark_contract.py tests/test_agent_runtime.py
PYTHONPATH=. python benchmarks/run_deterministic.py --output benchmark-report.json
```

The executable runner exercises:
1. Normal task completion with deterministic provider and tool fixtures.
2. A prohibited policy action, which must not execute.
3. A failing tool with a bounded retry budget, ending in failure.
4. Failed latest-commit CI evidence, which must block task success.
5. An expired worker lease, which must recover to queued while retry budget remains.

The runner also evaluates five independently labeled policy cases. It fails closed if any lifecycle scenario has an unexpected outcome, policy accuracy is below 100%, or a prohibited action is allowed.

GitHub Actions runs lint, compile, the deterministic pytest suite, and the executable report generator. Configure the check named **Deterministic agent regression benchmark** as a required status check in repository branch protection; workflow presence alone does not make it a protected-branch requirement.

## Model benchmark (separate, repeated, non-blocking)

The optional model runner executes the same five lifecycle scenarios through the real configured OpenAI-compatible provider while all repository tools remain deterministic local fixtures; it never writes to a real GitHub repository. It measures end-to-end task success, first-attempt success, policy handling, p95 latency, retries, provider usage, and cost when both token usage and price rates are supplied.

Run manually:

```bash
export RUSA_AGENT_MODEL_BASE_URL="https://your-model-endpoint/v1"
export RUSA_AGENT_MODEL_NAME="your-model-name"
export RUSA_AGENT_MODEL_REVISION="immutable-provider-revision"
export RUSA_AGENT_MODEL_API_KEY="..." # when required
export RUSA_AGENT_MODEL_INPUT_USD_PER_1M="..." # optional
export RUSA_AGENT_MODEL_OUTPUT_USD_PER_1M="..." # optional
PYTHONPATH=. python benchmarks/run_model_benchmark.py --repetitions 10
```

Or dispatch **Agent Evaluation Benchmark** in GitHub Actions with `run_model_benchmark=true`. Configure the corresponding repository secrets:
`RUSA_AGENT_MODEL_BASE_URL`, `RUSA_AGENT_MODEL_NAME`, `RUSA_AGENT_MODEL_REVISION`, optional `RUSA_AGENT_MODEL_API_KEY`, and optional input/output USD-per-million token rates.

The runner writes an aggregate JSON report and raw per-attempt JSONL. It requires a model revision and rejects a policy version that does not match the policy code. It records provider token usage only when the response reports prompt, completion, and total tokens. Cost per task remains `null` unless every provider call for every sampled task reports usage and both price rates are configured.

Run at least 10 repetitions per case for an exploratory baseline candidate and 30+ per case before promotion decisions. The model job is manually invoked and is not a merge-blocking check. Do not compare runs unless dataset version, model revision, policy version, provider/hardware configuration, and evaluation settings match.

## Metric definitions

- Task success rate = succeeded tasks / terminal tasks.
- First-attempt success rate = tasks succeeding on attempt 1 / terminal tasks.
- Policy decision accuracy = correct allow/deny/approval outcomes / independently labeled policy decisions. Report false-allow and false-deny counts separately.
- p95 latency = nearest-rank p95 of end-to-end terminal task duration. Always include the latency sample size.
- Retry rate = tasks with attempts greater than one / all sampled tasks; retry count = sum(attempts - 1).
- Cost per task = provider-reported cost sum / completed tasks. Never infer cost from prompt length.

For the deterministic fixture report, negative scenarios intentionally end in failed/blocked states; use `scenario_pass_rate` to judge whether expected failure handling worked. Do not interpret its task success rate as the expected pass rate of the whole scenario matrix.

## Initial regression thresholds (provisional)

These are proposed guardrails, not measured production baselines:
- Deterministic gate: 100% pass; any failure blocks merge.
- Policy false-allow: 0; any prohibited action allowed blocks merge.
- Model task success and first-attempt success: maximum 5 percentage-point regression against a comparable pinned-model baseline.
- p95 latency: maximum 20% regression against a comparable provider/hardware baseline.
- Retry rate: maximum 10 percentage-point regression.
- Cost per task: maximum 15% regression when provider usage and price data are available.

Do not apply model thresholds until a real baseline with matching dataset, model revision, policy version, and provider/hardware configuration exists. Until then, model status is `BASELINE_PENDING`.

## Metric availability

The operational endpoint `/v1/agent/evaluation/metrics` exposes tenant-scoped task success, first-attempt success, retry count/rate, nearest-rank p50/p95 latency, audited policy-decision counts, labeled policy accuracy, model identity, and explicit unmeasured values.

Policy accuracy remains `null` unless independent labeled `policy.benchmark_decision` events exist. Provider cost remains `null` until input/output token usage and pricing are persisted per task. Never publish zero for an unmeasured metric.
