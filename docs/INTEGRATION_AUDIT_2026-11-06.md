# RUSA-AI Integration Audit — 2026-11-06

## Execution policy

- Work mode: autonomous, phase-gated.
- Merge policy: explicit owner approval required before any merge to `main`.
- Deployment target: staging first; production deployment is out of scope until staging gates pass.
- No production secrets, user data, or model weights are to be committed.

## Phase 1 result: repository and pull-request topology

Repository: `StrategyG/RusaAi`  
Default branch: `main`  
Observed main SHA at audit time: `fc27f227f7ab5d43135f9bb2d074c208bfbb956d`

Open draft pull requests observed:

| PR | Head | Base | Head SHA | Changed files |
|---|---|---|---|---:|
| #17 RAG/MCP | `feat/rag-mcp-enterprise-20261106` | `feat/aicode-agent-production` | `42f4bcc85e8bc9d2c5ee0cdeebe96a07bfac504f` | 47 |
| #18 UI/Admin | `feat/ui-user-admin-workspace` | `feat/enterprise-agent-production-runtime` | `fe28afa5bb3f8ce4c2d45efe63febfcccb468032` | 33 |
| #19 Task lifecycle | `feat/agent-task-lifecycle-secure` | `fix/agent-lease-fencing-rebased` | `83d66fff041b2cf4c1d4521b90b6439fc616206a` | 11 |
| #20 Agent runtime | `feature/enterprise-agent-runtime-integration` | `main` | `ce0fd88e1d97b1828c9aafe4cd31c49e1a2648ab` | 35 |

All four PRs were open and marked draft at the time of inspection. GitHub reported each PR individually mergeable, but this is not evidence that the combined system is compatible.

## Critical integration finding

The PRs are not a linear dependency chain. Their bases represent different implementation histories. Pairwise GitHub comparisons reported divergent histories between the RAG/MCP, UI/Admin, secure task-lifecycle, and standalone agent-runtime branches. Several core files are independently changed across these lines, including:

- `llm-project/serve/app.py`
- `docker-compose.yml`
- `.env.example`
- `.github/workflows/ci.yml`
- `rusa_agents/orchestrator.py`
- `rusa_agents/worker.py`
- `rusa_agents/queue.py`
- `rusa_agents/policy.py`
- `rusa_agents/contracts.py`

Therefore, directly merging all four PRs is unsafe. Integration must be performed as a deliberate port/rebase with API, schema, dependency, and runtime contract checks. PR #20 is an alternative runtime implementation and must be compared against the secure lifecycle implementation before any of its overlapping runtime code is adopted.

## Provisional integration baseline

A working branch was created:

`integration/staging-baseline-2026-11-06`

It starts from PR #19 head:

`83d66fff041b2cf4c1d4521b90b6439fc616206a`

This is a provisional engineering baseline, not a merge decision. It is selected to preserve the newer tenant task-lifecycle and worker-lease hardening while the competing runtime implementations are reviewed. It does not yet include the UI/Admin or RAG/MCP feature sets.

## CI evidence observed

At audit time, GitHub Actions had multiple queued/in-progress/cancelled runs on the integration-related branches. A completed Agent Runtime CI run on an earlier PR #20 head failed in the `agent-tests` job at Ruff lint:

- `rusa_agents/audit.py`: unused `sqlalchemy.func`
- `rusa_agents/storage.py`: unused `sqlalchemy.select`
- `rusa_agents/worker_cli.py`: multiple imports on one line

Compile and test steps were skipped after the lint failure. This is a confirmed CI defect on that earlier PR #20 commit, not proof that the latest head has the same failure. Re-run current checks before drawing a current pass/fail conclusion.

## Phase 1 exit criteria

- [x] Inspect default branch and open PR metadata.
- [x] Record PR heads, bases, and changed-file counts.
- [x] Compare branch histories and identify divergence.
- [x] Inspect a concrete CI failure log.
- [x] Create an isolated staging integration branch.
- [ ] Reconcile the agent runtime implementation.
- [ ] Port and test RAG/MCP against the chosen runtime contracts.
- [ ] Port and test UI/Admin against the chosen API contracts.
- [ ] Run full CI, integration, security, and staging deployment checks.

## Next actions

1. Review the secure lifecycle baseline and its latest CI evidence.
2. Compare PR #20 runtime contracts with PR #19 before selecting reusable components.
3. Port RAG/MCP in small, isolated commits; do not overwrite core runtime files wholesale.
4. Port UI/Admin and explicitly reconcile auth, task, and chat API contracts.
5. Run PostgreSQL/pgvector, MCP protocol, frontend build/E2E, tenant-isolation, and compose smoke tests.
6. Open a draft integration PR for owner review. Do not merge to `main` without explicit approval.

## Production boundary

This branch is not production-ready and must not be deployed to production. Staging is allowed only after build, migration, secrets, health/readiness, and security gates are verified. Real model artifact, license, checksum, target hardware, quality benchmark, backup/restore drill, and operational sign-off remain release requirements.
