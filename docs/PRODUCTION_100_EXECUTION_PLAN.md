# RUSA-AI production excellence — performance-first execution plan

Status: **integration work in progress; NOT production approved**.
Canonical working branch: `integration/production-100-performance-20261106`.
Base snapshot: `integration/staging-baseline-2026-11-06` at `e9fdb98d0814f4a0e7b6e1e10ab1f2004683e70c`.
Protected boundary: **no merge to main and no production deployment without owner approval**.

## Verified branch audit (2026-11-06)

- `main`: `fc27f227f7ab5d43135f9bb2d074c208bfbb956d`; CI Python tests failed.
- Staging baseline was 271 commits ahead of main, zero behind, when compared.
- PR #3: hardened inference foundation; successful CI on its checked commit.
- PR #17: tenant-scoped RAG/MCP; diverged from staging; **do not blindly merge**.
- PR #18: web workspace and identity; successful CI on checked commit; staging already contains related files.
- PR #19: task lifecycle controls; successful CI on checked commit; stacked branch.
- PR #20: alternative agent runtime; diverged from staging; latest runs pending/cancelled at audit time.
- Staging latest CI was pending at audit time; do not infer pass from older successful runs.

## Architecture decision record ADR-001

Keep the staging baseline as the **canonical integration starting point**, not the final architecture.
Preserve the existing `rusa_agents`, `rusa_knowledge`, `rusa_mcp`, `apps/web` contracts until comparative tests establish the preferred implementation. Compare PR #20 agent runtime against the baseline for throughput, correctness, tenant isolation, and operational complexity; adopt only independently validated improvements. Never mix migration histories or embedding dimensions without an explicit conversion plan and rollback.

## Work packages and acceptance gates

| ID | Work package | Measurable acceptance |
| --- | --- | --- |
| P0 | Stabilize integration CI | Fresh HEAD: lint, tests, dependency audit, image builds and smoke tests all pass; failures triaged |
| P1 | Establish repeatable performance baseline | Report git SHA, hardware, model, dataset, load, concurrency, warmup, p50/p95/p99, throughput, error rate |
| P2 | Optimize inference | Real local model; latency and tokens/sec meet approved SLO under target concurrency, no mock fallback |
| P3 | Optimize queue and agent runtime | Durable leases, idempotency, bounded retries, worker recovery, throughput and queue-age SLO |
| P4 | Optimize RAG/MCP | Tenant-safe retrieval; benchmark recall/precision and p95 latency, bounded MCP timeouts |
| P5 | Cloud and on-prem deployment parity | Version-pinned images; resource sizing, TLS, secrets, storage, GPU/CPU profiles, rollback tested |
| P6 | Security and quality gates | RLS and cross-tenant tests; no unmitigated critical/high findings; full end-to-end regression |
| P7 | Observability and resilience | Metrics/traces/logs, alerts, backup restore drill, documented RTO/RPO, failure injection |
| P8 | Release evidence | Signed release manifest, SBOM, test report, load-test report, rollback rehearsal and explicit approval |

## Performance benchmark policy

Use the standard-library smoke benchmark in `scripts/perf/http_probe.py` for **local/staging only**.
It defaults to 10 requests at concurrency 2 and never targets production by default.
It records latency distribution, request throughput, and HTTP error rate; it does **not** measure model tokens/sec or replace k6/Locust-style sustained load testing.
Do not declare an SLO pass until the owner supplies target hardware, model, target concurrency, workload and SLO.
Initial candidate SLOs (proposals, not accepted thresholds): API health p95 < 250 ms under 20 concurrent requests; error rate < 0.1%; task queue age p95 < 10 s under provisioned capacity. Model inference latency/TTFT must be defined against the selected model and GPU.

## Scoring policy

100/100 is a rubric result, **not a guarantee of zero risk**. No points are awarded without commit-linked evidence.
Block release regardless of aggregate score if critical security, tenant isolation, real inference, data restore, or production rollback gates fail.
Re-score only after a fresh green CI run on the exact release candidate SHA.

## Execution log

1. [x] Re-audit active branches and CI.
2. [x] Create isolated canonical integration working branch.
3. [x] Add performance benchmark and execution plan.
4. [ ] Obtain fresh passing CI on the new branch.
5. [ ] Collect representative hardware/model and workload requirements.
6. [ ] Implement measured performance improvements and regression tests.
7. [ ] Validate cloud and on-prem deployment profiles.
8. [ ] Owner approves any main merge or production deployment.
