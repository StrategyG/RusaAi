# RUSA-AI agent production-readiness review — 2026-11-06

Repository: StrategyG/RusaAi  
Working branch: fix/agent-validation-review  
Stacked pull requests: #7 (hierarchical agent runtime) and #8 (runtime hardening).  
Review objective: close the highest-risk gaps without weakening autonomy boundaries.

## Implemented in this review revision

| Area | Change | Regression evidence added |
|---|---|---|
| Worker leases | Unique worker identity, heartbeat, lease expiry, fenced checkpoint/retry/acknowledgement | PostgreSQL stale-owner test |
| Retry correctness | Failed retry clears terminal checkpoint so the next attempt can restart instead of replaying a terminal FAILED state | Queue behavior and worker integration path |
| Stage recovery | Completed worker results are reused after restart; a paused external CI stage is explicitly refreshed | Checkpoint/restart test |
| Idempotency | Duplicate key must match the same semantic payload; generated task ID and timestamp are normalized | PostgreSQL duplicate/conflict test |
| Tenant isolation | RLS readiness checks for task and memory tables; memory insert sets tenant context | PostgreSQL RLS and readiness tests |
| Repository scope | Exact global owner/name allowlist plus per-tenant repository assignment enforced at API and GitHub gateway | API and gateway regression tests |
| Branch isolation | Server-derived branch is unique to tenant plus idempotency identity; arbitrary branch reuse rejected | Task contract and API tests |
| HTTP intake | Actual streamed body bytes bounded before JSON parsing; request timeout remains 15 seconds | Chunked body and replay tests |
| External responses | GitHub and provider bodies are streamed with byte limits | Adapter limits and configuration |
| CI completeness | Bounded check-run pagination, stable total count, unique run IDs, complete-set validation | Incomplete and paginated check-run tests |
| Security review | Security worker fetches evidence, then runs a second read-only model pass that actually receives the complete diff and CI result | Review-only worker and orchestrator tests |
| Secret handling | Common secret formats are redacted; diffs containing redaction markers are blocked | Redaction and hard-gate tests |
| Commit freshness | Tester CI, Security CI, and Security diff must identify the same head SHA | Changed-head regression |
| Diff completeness | Security context is capped at 20 files and bounded patch size; incomplete diffs are blocked | Oversized and protected-path diff tests |
| CI triggers | Pull-request checks now run for stacked PRs regardless of target branch | Workflow contract test |

## Current test status

The earlier review revision recorded 60 passed and 3 skipped, with PostgreSQL integration tests skipped locally because no RUSA_TEST_DATABASE_URL was configured. That result applies to the earlier commit, not automatically to the changes in this revision.

This revision adds or updates regression tests for the new controls. It must be validated by the GitHub Actions run on the latest branch head. A test authored but not executed is not evidence of a passing test.

## Verification still required

- [ ] GitHub Actions test job passes on the current branch head.
- [ ] PostgreSQL integration tests execute against the CI PostgreSQL service, not skip.
- [ ] Both container images build; inference API smoke test passes.
- [ ] Docker Compose validation succeeds with generated secrets and a real model directory.
- [ ] Live GitHub App/token permissions match the global allowlist and can read checks, create branches, write contents, and open draft PRs.
- [ ] Tenant repository assignments match business ownership and are tested with separate tenant credentials.
- [ ] Live provider returns valid structured JSON under configured response and timeout limits.
- [ ] Multi-worker chaos test proves heartbeat, lease expiry, stale-owner rejection, and idempotent replay.
- [ ] Prompt-injection, secret scanning, dependency, and repository-diff security tests pass.
- [ ] TLS gateway, distributed rate limits, egress allowlists, PostgreSQL TLS/encryption/PITR, and immutable audit export are configured.
- [ ] Staging load/soak, incident response, restore, and rollback drills are approved.
- [ ] Model evaluation dataset and quality/safety acceptance thresholds are defined and met.

## Production decision

NOT YET CERTIFIED PRODUCTION-READY. The repository now has stronger code-level controls, but a production decision requires the verification evidence above. No merge to main, deployment, release, or live credential configuration is implied by this review.


## CI trigger correction

The stacked PR target branch originally filtered pull-request events to main only, so PR #8 targeting feat/aicode-agent-production had no PR-triggered workflow run. The target branch workflow has now been updated to accept pull requests against all branches. A push-triggered CI run on the updated target branch is in progress; the PR #8 head is being advanced to request its own PR checks against the corrected target workflow.
