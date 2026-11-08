# RUSA-AI Benchmark v2

This benchmark layer adds a reproducible, repository-owned reasoning smoke suite
and documents the existing streaming inference/performance runner. It separates
test execution time from model quality and serving capacity.

## Benchmark dimensions

| Category | Evidence / runner | What the result means |
|---|---|---|
| RAG | `rusa-knowledge` retrieval evaluation | Recall@K, MRR@K and NDCG@K on a versioned retrieval dataset |
| Inference | `scripts/benchmark_inference.py` | Streaming request throughput, completion tokens/sec, p50/p95/p99 latency and TTFT |
| Reasoning | `scripts/benchmark_reasoning.py` | Normalized exact-match accuracy on the versioned reasoning smoke dataset |
| Multi-agent | A2A, orchestration and worker integration tests | Contract and workflow correctness; add measured handoff/task success for load campaigns |
| Autonomy | Queue, lease, retry, cancellation and recovery tests | Durable execution and fail-closed behavior |
| Security | Ruff/Bandit/Trivy and tenant-isolation tests | Static and image findings plus authorization/isolation evidence |
| Performance | `scripts/benchmark_inference.py` | Serving capacity; CI wall-clock duration is not request throughput |

## Reasoning smoke benchmark

The committed dataset is `llm-project/data/reasoning_smoke.jsonl`. It contains
short, deterministic arithmetic, logic, pattern, instruction-following and
evidence-based decision cases. Answers are scored using normalized exact match
against each case's `accepted_answers`.

Install the project and development dependencies, then run a local API:

```bash
python -m pip install -e ".[dev]"
RUSA_API_KEY="$RUSA_API_KEY" python scripts/benchmark_reasoning.py \
  --base-url http://127.0.0.1:8000 \
  --model rusa-ai \
  --concurrency 2 \
  --min-accuracy 0.90 \
  --output artifacts/reasoning-benchmark.json
```

The runner refuses non-local targets unless both
`--allow-remote-staging` and `--environment staging` are supplied. It requires
HTTPS for non-local targets and does not support production endpoints.

A report includes the dataset SHA-256, source commit, model identifier,
per-category accuracy, individual case outcomes, request failures and an
explicit acceptance decision. Do not publish API keys or private prompts in
benchmark artifacts.

### Reasoning metric boundary

This runner measures **closed-answer exact-match accuracy only**. It does not
claim to measure general reasoning quality, groundedness, citation correctness,
calibration or hallucination rate. Those dimensions require separately reviewed
datasets, evidence annotations and independent human or model-judge protocols.
Do not infer those metrics from the exact-match score.

## Inference and performance benchmark

The repository's `scripts/benchmark_inference.py` is the serving benchmark.
It uses streaming chat completions and reports request throughput, completion
tokens/sec, latency percentiles and time-to-first-token.

Example local run:

```bash
RUSA_API_KEY="$RUSA_API_KEY" python scripts/benchmark_inference.py \
  --base-url http://127.0.0.1:8000 \
  --requests 100 \
  --concurrency 10 \
  --environment local \
  --output artifacts/inference-benchmark.json
```

For a staging target, add `--environment staging --allow-remote-staging` and
use an HTTPS staging URL. The runner intentionally refuses production targets.
Run multiple repetitions with the same model artifact, hardware, prompts,
concurrency and generation limits. Record server hardware/model manifest
separately; client hardware metadata is not a substitute for server telemetry.

A bounded benchmark is not a soak test. Before production decisions, add
staging campaigns at increasing concurrency and sustained duration, and
collect server CPU/RAM/VRAM, queue depth, error rate, saturation and recovery
signals alongside client latency.

## Scoring and release gate

Seven category scores are aggregated on a 0..100 scale using these weights:

| Category | Weight |
|---|---:|
| RAG | 15% |
| Inference | 15% |
| Reasoning | 20% |
| Multi-agent | 15% |
| Autonomy | 15% |
| Security | 10% |
| Performance | 10% |

`rusa_agents.benchmarking.aggregate_benchmark_scores` rejects missing,
unexpected or out-of-range category scores. The security category has a
mandatory 95/100 hard gate: a weighted score can still be reported, but the
result is not release-eligible when security is below that threshold.

A category must not be assigned a score from another repository's run, from a
workflow duration, or from a target that was not actually tested. Mark missing
evidence as not measured; do not impute it as zero or as a pass.

## Evidence required for each run

Retain, at minimum:

- repository and exact commit SHA;
- workflow run ID (when executed in CI);
- benchmark ID and dataset SHA-256;
- model name, immutable model manifest digest and serving configuration;
- client and server hardware/runtime metadata;
- concurrency, request count, warm-up policy and duration;
- successes, failures, timeouts and retry policy;
- p50/p95/p99 latency and throughput;
- raw machine-readable report and evaluator version.

Never claim production certification from CI smoke tests alone. Production
readiness also requires staging soak, security review, backup/restore and
rollback drills, observability, SLO approval and operator sign-off.
