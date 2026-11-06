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
| Tenant isolation | RLS readiness checks for task and memory tables; memory insert sets tenant context; API tenant map must match the credential map exactly | PostgreSQL RLS and API isolation tests |
| Repository scope | Exact global owner/name allowlist plus per-tenant repository assignment enforced at API and GitHub gateway | API and gateway regression tests |
| Checkpoint privacy | Raw read/write file contents are omitted from durable checkpoints; content hashes and sizes are retained; memory and audit details redact secrets | Checkpoint and HMAC audit redaction tests |
| Lease fencing | Worker checks current lease ownership before every repository tool call; heartbeat interval tightened to one-sixth of lease duration | Stale lease tool-guard and PostgreSQL lease tests |
| GitHub write idempotency | Ambiguous Contents API write responses are reconciled by reading the target branch file before retrying | Ambiguous commit recovery test |
| Inference request limits | Streamed/chunked request bodies are bounded before JSON parsing on both inference and agent APIs | Inference and agent ASGI middleware tests |
| Protected operations | Dependency manifests, lockfiles, container files, infrastructure, deployment, and GitHub configuration writes require human approval | Policy path approval tests |
| CI configuration | Required GitHub check names are configured as a validated JSON array; complete check-run sets are preserved in checkpoints | Worker configuration and checkpoint tests |
| Branch isolation | Server-derived branch is unique to tenant plus idempotency identity; arbitrary branch reuse rejected | Task contract and API tests |
| HTTP intake | Actual streamed body bytes bounded before JSON parsing; request timeout remains 15 seconds | Chunked body and replay tests |
| External responses | GitHub and provider bodies are streamed with byte limits | Adapter limits and configuration |
| CI completeness | Bounded check-run pagination, stable total count, unique run IDs, complete-set validation | Incomplete and paginated check-run tests |
| Security review | Security worker fetches evidence, then runs a second read-only model pass that actually receives the complete diff and CI result | Review-only worker and orchestrator tests |
| Secret handling | Common secret formats are redacted; secret-bearing writes are blocked before GitHub access; checkpoints, memory, and audit details omit/redact sensitive content | Redaction, checkpoint, and hard-gate tests |
| Commit freshness | Tester CI, Security CI, and Security diff must identify the same head SHA | Changed-head regression |
| Diff completeness | Security context is capped at 20 files and bounded patch size; incomplete diffs are blocked | Oversized and protected-path diff tests |
| CI triggers | Pull-request checks now run for stacked PRs regardless of target branch | Workflow contract test |

## Current test status

- **PR #11 CI migration:** GitHub Actions run 42182286658 passed on commit 3025cf04b3085aa7481610639ddb512b3354b01f. This validates the default-branch CI migration and the two corrected legacy API fixtures. The agent-specific image was not present in this PR branch and was therefore skipped.
- **PR #7 implementation baseline:** GitHub Actions run 42183099336 passed on commit 6857a5c7a3060fea4eed9f5979e4b1e26bf95221. Lint, compile, tests, dependency audit, inference image build, agent image build, and inference liveness smoke test all passed.
- **PR #8 hardening branch:** current head is 9740661f71e31b2669d1527e713a294604794bf2. It remains a draft PR and does not yet have a verified full CI result for this head. The additional tests authored in this revision must not be counted as passing until a workflow run executes against this exact commit.

A passing PR #7 run validates the implementation baseline, not the newer PR #8 hardening changes. The PR #8 branch still requires its own full CI run, including PostgreSQL integration tests and both container builds.

## Verification still required

- [x] GitHub Actions baseline test job passes on PR #7 head 6857a5c7a3060fea4eed9f5979e4b1e26bf95221.
- [x] PostgreSQL integration tests execute against the CI PostgreSQL service on PR #7, not skip.
- [x] Both container images build; inference API smoke test passes on PR #7.
- [ ] Full GitHub Actions suite passes on PR #8 head 9740661f71e31b2669d1527e713a294604794bf2 after the CI migration is approved/merged and the draft PR is made ready for checks.
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

NOT YET CERTIFIED PRODUCTION-READY. The repository now has stronger code-level controls and a passing CI baseline on PR #7, but the newest PR #8 hardening changes are not yet CI-verified. A production decision requires the remaining verification evidence above. PR #11 is a CI migration PR and PR #8 is still draft; no merge to main, deployment, release, or live credential configuration is implied by this review.


## CI trigger correction

The stacked PR target branch originally filtered pull-request events to main only, so PR #8 targeting feat/aicode-agent-production had no PR-triggered workflow run. The target branch workflow has now been updated to accept pull requests against all branches. A push-triggered CI run on the updated target branch is in progress; the PR #8 head is being advanced to request its own PR checks against the corrected target workflow.
