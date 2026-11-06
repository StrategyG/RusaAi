# Agent runtime evaluation

This suite targets the integrated staging runtime. Its parent baseline is
`7c74e9b86c4f4cb2c217f6389e01d0ede37e0e54`. The benchmark commit leaves the
runtime and policy untouched, so its first completed run is the baseline for
this dataset. Each JSON report records the tested checkout SHA, requested model
version, SHA-256 of the policy implementation, dataset version and sample sizes.
Use a clean checkout and compare results only within the same dataset and mode.

## Merge gate

The existing `Lint, test, and audit dependencies` job runs the scripted suite
against the CI PostgreSQL service. Any mismatch exits nonzero. Missing database
configuration fails when `--require-postgres` is supplied. Administrators can
verify that the job is required by branch protection on the target branch.

```bash
PYTHONPATH=. RUSA_BENCH_DATABASE_URL="$TEST_DATABASE_URL" \
  python tests/benchmark_runtime.py --require-postgres --repeats 5
```

Use an empty, dedicated test database with a role that can initialize the queue
and memory schemas. The crash scenario starts a real worker subprocess, exits it
after a committed analyst checkpoint, expires the lease in PostgreSQL, and runs
a replacement worker. Success requires exactly two claims and reuse of the
checkpoint without rerunning the analyst. Forcing expiration keeps CI fast and
leaves natural crash recovery latency unmeasured.

| Scripted scenario | Expected outcome | Samples in CI |
| --- | --- | ---: |
| Normal | Succeeds without repair | 5 |
| Policy denial | Protected write waits for approval and never calls the gateway | 5 |
| Tool failure | Unsuccessful gateway result blocks the task | 5 |
| QA failure | Failing CI blocks the task after two repair cycles | 5 |
| Worker crash | Replacement succeeds with one reclaim and no repair | 5 |

Five hand-labeled allow, approval and hard-deny policy decisions run on each
iteration, for 25 observations across five unique cases. GitHub evidence and
worker outputs in scripted mode are synthetic, with no external writes.
Any mismatch is a deterministic regression. Expected conformance and policy
accuracy are 100%. Across the ten tasks expected to complete, the expected task
success is 10/10 and first-attempt success is 5/10 because the other five require
recovery by design. Negative controls are counted separately in conformance.
Queue retries count extra claims. QA repair cycles are tracked separately.
The report uses nearest-rank p95 per scenario. With five samples, p95 is the
maximum and is descriptive only.

## Repeated real-model benchmark

Use a versioned model ID and an explicitly configured OpenAI-compatible backend.
This sends synthetic prompts to that backend and uses local fake GitHub tools.
Use the same version and runner conditions across comparisons.

```bash
export RUSA_BENCH_MODEL_VERSION=your-versioned-model-id
export RUSA_BENCH_MODEL_BASE_URL=https://your-backend.example/v1
# Supply RUSA_BENCH_MODEL_API_KEY through your environment.
PYTHONPATH=. python tests/benchmark_runtime.py --model --repeats 30
```

The real model drives the normal orchestration path. Scripted fault cases and
policy decisions remain in the merge gate. The model run is advisory and reports
raw samples for variability analysis. The provider adapter currently exposes
completion text only. Actual provider revision, token usage, and cost per task
are unavailable. Reports represent missing measurements explicitly.

Collect a 30-run baseline before comparing candidates. Initial review thresholds
are a decrease of at least 10 percentage points in task or first-attempt success,
or an increase of at least 20% in p95 under matched conditions. Treat these as
provisional review signals. Repeat a flagged run and report binomial confidence
intervals for success rates before promoting thresholds to operational gates.
Recovery latency needs a separate run with natural lease expiration.
