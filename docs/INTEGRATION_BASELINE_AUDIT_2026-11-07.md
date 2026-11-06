# Integration baseline audit — 2026-11-07

## Critical correction to the earlier integration plan

A direct GitHub compare of each PR head against the current `main` shows that
these are not ordinary merge-conflict-only branches. All three source heads
are based on a much older common ancestor and have diverged substantially:

| Source | Commits ahead of main | Commits behind main | Compare status |
|---|---:|---:|---|
| PR #40 head | 165 | 512 | diverged |
| PR #24 head | 84 | 512 | diverged |
| PR #33 head | 274 | 512 | diverged |

The common merge base reported by GitHub is
`fc27f227f7ab5d43135f9bb2d074c208bfbb956d`.

This means merging any source branch wholesale risks reverting or replacing
hundreds of newer mainline commits. The earlier proposal to simply merge
#40, then #24, then #33 is unsafe and is superseded by this audit.

## Confirmed shared-file incompatibilities

- `llm-project/serve/app.py`: main has 889 lines; PR #40 485; PR #24 776;
  PR #33 1,453. Each has a different application structure. PR #33 includes a
  Transformers adapter but explicitly returns a 501 for streaming requests.
  PR #24 contains a separate `model_runtime.py` with manifest verification,
  backend loading, and streaming-related support. A safe integration requires
  porting selected behavior into the current main API, not replacing the file.
- `rusa_agents/queue.py`: main has 483 lines; PR #40 691; PR #33 790.
  PR #33 extends the PR #40-style queue, but both are structurally different
  from main. Port and test idempotency/fenced lease semantics against current
  main schema and APIs.
- `rusa_agents/worker.py`: absent on main; PR #40 and #33 both add versions.
  PR #33's version is longer and should be reviewed for all PR #40 race fixes.
- `Dockerfile`: main uses Python 3.12 slim-trixie; source PRs use Python 3.11
  slim and different build/runtime dependency strategies. Preserve main's
  hardened baseline unless dependency compatibility proves otherwise.
- `.github/workflows/ci.yml`: main has security static analysis, sandbox
  tests, Trivy image reporting and blocking, and production Compose validation.
  PR #33's workflow replaces these with feature tests and builds for RAG/MCP
  and web. The final workflow must be additive, not a replacement.

## Revised safe integration method

1. Keep the integration branch based on current main.
2. Do not merge source PR branches or cherry-pick their full histories.
3. Port narrowly scoped feature commits/files from PR #40, #24, and #33 into
   current main APIs, preserving current contracts and hardening.
4. Prefer PR #33's superset agent/RAG/MCP files only after proving its queue,
   worker, and API behavior retains all PR #40 race/idempotency guarantees.
5. Port PR #24's model runtime as an isolated module, then adapt the current
   main `app.py` through its existing backend injection/fail-closed contract.
6. Add streaming only if tests demonstrate correct cancellation, limits,
   auth, error handling, and cleanup.
7. Extend main CI without removing any existing gate. Add dependency audits,
   feature-specific PostgreSQL tests, model-manifest tests, and web/RAG/MCP
   image checks as separate jobs where feasible.
8. Run full tests, build and security scans on the exact integration SHA.
9. Open a fresh PR from the integration branch to current main for review.
10. Merge only after all required checks and review approvals pass.

## Current branch state

The integration branch contains the planning/audit documentation only.
No source PR application code has been imported, no merge has been attempted,
and main remains unchanged. This is intentional: GitHub metadata and file
fetches alone cannot prove that a large cross-version application port works.
