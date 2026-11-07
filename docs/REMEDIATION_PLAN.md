# RUSA-AI remediation plan

- Date: 2026-11-07
- Baseline: `StrategyG/RusaAi` at commit `3c012e0a4342b8e10b974643884539618d222aee` (`main`)
- Status: proposal for owner review

## 1. Score calibration

| Dimension | Submitted | Calibrated | Rationale |
|---|---|---|---|
| Software architecture | 7.5 | 7.5 | Clear boundaries between API, worker, sandbox, and MCP. The database is the source of truth, and tenancy is enforced with row-level security. Deductions: two parallel audit mechanisms, and an embedding endpoint that defaults to a third-party vendor. |
| Code structure and modularity | 7.5 | 7.0 | `app.py` (~38 KB), `queue.py` (~30 KB), and `git_workspace.py` and `tool_gateway.py` (~24 KB each) carry many responsibilities. The protected-path lists are duplicated in two modules. |
| Main-branch security | 5.0 | 4.0 | PR #49 (more than 100 changes) was merged with no recorded formal review. No CODEOWNERS file existed before this plan. Branch protection cannot be verified from the repository. Moves to 5 once an enforced ruleset is confirmed. |
| Reliability and recovery | 5.5 | 5.0 | Lease fencing and race tests are strong. Publication runs inside an open transaction, cancellation can block, the worker kill switch is read once at startup, and there is no backup or restore evidence. |
| AI and model maturity | 3.0 | 3.0 | No model weights, no versioned labeled dataset, and no measured agent quality. The score is 3 only because the evaluation code exists. |
| Enterprise readiness | 3.5 | 3.5 | Release gates in the production ledger are pending. No SSO, no TLS in Compose, no high availability in the reference deployment, and no retention or deletion policy in the code reviewed. |
| Overall | 5.5 | 5.0 | Mean of the six calibrated dimensions. |

These scores are the baseline. A score changes only after the evidence in section 2 is linked from this file.

## 2. Targets and evidence

| Dimension | After Phase 1 | After Phase 2 | Evidence required |
|---|---|---|---|
| Main-branch security | 7 | 8 | Exported ruleset with required checks and code-owner review; a second human reviewer; secret scanning and push protection enabled |
| Reliability and recovery | 6.5 | 8 | Tests that kill the worker during publication and drop the database connection, with no duplicate PRs; restore drill report; active alerts |
| Code structure and modularity | 7.0 | 8.0 | Module split merged with the test suite green; one source of truth for protected paths |
| Software architecture | 7.5 | 8.5 | One audit mechanism in production; no vendor default for embeddings; control and execution planes separated in code |
| AI and model maturity | 3.5 | 6.0 | Labeled RAG evaluation set with confidence intervals; agent benchmark results; latency and quality results for one pinned model |
| Enterprise readiness | 4.5 | 6.5 | TLS ingress, two API replicas, managed PostgreSQL with point-in-time recovery, restore drill, tenant retention and deletion, SSO for administrators |

The Phase 1 and Phase 2 values are estimates. They are not earned until the evidence exists.

## 3. Phases

### Phase 0 — in the change package
- `.github/CODEOWNERS` and `.github/pull_request_template.md`.
- `AutonomyPolicy` denies all actions when an empty allowlist is passed explicitly.
- `tests/test_policy_fail_closed.py`: regression tests for that change.
- Production ledger: RAG migration reference corrected from `005` to `007`.
- This plan.

### Phase 1 — defaults and repository settings (1–2 weeks)
1. Create a ruleset for `main`: require pull requests; require the three CI jobs (`Test, lint, and security`, `Build, test, and scan production images`, `RAG and MCP integration tests`); require branches to be up to date; block force pushes and deletions; require review from code owners. Allow bypass only for administrators, and log every bypass.
2. Add a second human reviewer. With one maintainer, "require approval" can only be satisfied by bypassing it.
3. Enable secret scanning, push protection, Dependabot alerts, and code scanning.
4. Restrict the GitHub token used by AICode to one repository, with `contents: write` and `pull_requests: write` only.
5. Make `ENVIRONMENT` required, with no default of `development`. Update the tests that depend on the default.
6. Make `RUSA_EMBEDDING_BASE_URL` empty by default, and fail closed when RAG is enabled without it.
7. Fix `require_task_permission` for development mode without API keys, where `request.state.principal` is never set. Add a test for that path.
8. Run CI on the same Python version as the production images (3.12). Pin third-party actions to commit SHAs.
9. Add `llm-project/serve` to the Bandit scope in CI.
10. Add a lock file with hashes, and pin base image digests.

### Phase 2 — reliability (4–6 weeks)
1. Move remote side effects out of the database transaction. Use a `publishing` state with a fencing token, a deterministic branch name per task, and a reconciliation step that checks the remote branch SHA and any existing PR before publishing again.
2. Set `lock_timeout` on cancel, retry, and heartbeat statements.
3. Keep attempt history. Stop clearing `result` and `last_error` on retry. Replace retry-as-approval with an `approve` action bound to a specific commit SHA and PR.
4. Read the worker kill switch from a database flag on each loop iteration, and drain in-flight work on SIGTERM.
5. Run chaos tests: kill the worker during publication, drop the database connection, and simulate GitHub timeouts. Pass condition: no duplicate PRs.
6. Run a backup and restore drill, and record the result.
7. Add alerts for tasks running past their lease, an `awaiting_approval` backlog older than a threshold, audit verification failures, and RLS policy drift.

### Phase 3 — structure and architecture (6–10 weeks, incremental)
1. Split `app.py` into authentication, admission control, task routes, inference routes, and lifespan modules.
2. Split `queue.py` into schema, leases, idempotency, and policy audit.
3. Split `git_workspace.py` into workspace management, publishing, CI verification, and the GitHub client.
4. Keep one source of truth for protected paths, used by both the tool gateway and the publisher.
5. Use one audit mechanism in production. Keep JSONL for development only.
6. Replace the global `inference_provider` with dependency injection through `app.state`.
7. Package `rusa_mcp`, and remove `sys.path` manipulation.

### Phase 4 — evidence for AI and models (8–12 weeks)
1. Build a labeled RAG evaluation set in the form of the `rag-eval-v1` example in `copilot-instructions.md`, with a documented labeling method and confidence intervals.
2. Build an agent benchmark of 30–50 small tasks with ground-truth tests. Measure task success, unsafe-action rate, and the share of PRs accepted in review.
3. Select one open-weights model, pin its manifest, and measure p50/p95 latency, throughput, and memory on the target hardware.
4. Run a safety evaluation covering refusals and unsupported claims.
5. Store artifacts with hashes under `evidence/`, and link them from the production ledger.

### Phase 5 — enterprise (in parallel)
1. TLS ingress, two API replicas, managed PostgreSQL with point-in-time recovery.
2. Secrets from a KMS or Vault, not from `.env`.
3. SSO (OIDC) for administrators. Keep API keys for service clients.
4. Per-tenant retention and deletion for task payloads and memory. Audit records must be kept, so use pseudonymization where needed.
5. Service-level objectives and a tested runbook.
6. Data processing agreements with the model and embedding providers.

## 4. Decisions the owner must make
- License. The repository has no LICENSE file.
- Who the second reviewer is.
- Whether tenant data may leave the deployment for external model or embedding providers.
- Which model is selected for Phase 4.
- Retention periods for task payloads and audit records.

## 5. Completion rule
A score moves only when the evidence in section 2 is linked from this file. This plan does not raise any score by itself.

## 6. Limits of this review
- Repository settings (branch protection, rulesets, secret scanning) were not visible to the reviewer. Scores assume they are not enforced until proven.
- Only the policy regression test was run, locally, with a minimal runner because pytest is not available in the review environment. Results: 7 of 7 pass against the replacement `policy.py`; the empty-allowlist test fails against the previous `policy.py`. No other test was run. CI on any change is the verifier.

## 7. Implementation tracking — 2026-11-07

This section records delivered code and verification evidence. It does not change the calibrated scores in section 1; section 2 completion evidence is still required.

### Phase 0 and Phase 1 — code-level changes in PR #63

- Commit `8429360c0ecf13bd4ea7f781f49ff5f163938079` added the fail-closed policy correction, explicit environment validation, development principal fix, explicit RAG embedding endpoint, Python 3.12 CI alignment, immutable GitHub Action references, expanded Bandit scope, CODEOWNERS, pull-request checklist, and the migration-ledger correction.
- The publisher now requires the RAG/MCP integration job as well as the core and production-image jobs before accepting CI evidence.
- The sandbox compile, Ruff, and Bandit profiles cover the same application packages as CI.
- External Phase 1 gates remain open. The latest repository read reports `main.protected=false` and no repository rulesets. PR #63 has no submitted review. Secret scanning, push protection, Dependabot, code scanning, and token scope still require owner-side verification.

### Phase 2 — bounded queue lock waits delivered; reliability refactor remains open

- Commit `93f29e05963dd7187bb9ca45b875de8949e2ebc9` adds a 1.5-second PostgreSQL `lock_timeout` to cancel, retry, and heartbeat operations.
- Heartbeat treats PostgreSQL lock-timeout SQLSTATE `55P03` as a reason to re-check lease ownership without taking a row lock. This is a compatibility measure for the current publisher, which still holds the job row lock during the bounded GitHub push; it is not a substitute for moving remote side effects outside the transaction.
- Commit `00b86fed6a6f906937f7c962238725589fa2d3bd` makes the new PostgreSQL race tests claim their exact task IDs, avoiding interference from other tenants' queued fixtures.
- CI run [42372470632](https://github.com/StrategyG/RusaAi/actions/runs/42372470632) passed all three required jobs. The core test job reported 175 passed and 5 skipped; RAG/MCP integration, the full test suite in the isolated sandbox, production image builds, Trivy checks, and production Compose validation also passed.
- The follow-up CI run [42372769073](https://github.com/StrategyG/RusaAi/actions/runs/42372769073) also passed all three jobs on the documentation-updated head. Its Trivy report counted HIGH findings in the API (53), sandbox (58), worker (57), and RAG/MCP (53) images, with zero CRITICAL findings. The `--ignore-unfixed` gate passed; this means no fixable HIGH/CRITICAL findings blocked the build, not that the images are vulnerability-free. The remaining HIGH findings require triage, upstream monitoring, and a fresh scan after base-image updates.

### Phase 2 — still required

- Move GitHub publication outside the open PostgreSQL transaction, introduce a `publishing` state and fencing token, use deterministic per-task branch names, and reconcile remote branch SHA and existing PR after worker crashes or ambiguous GitHub responses.
- Preserve attempt history and prior result/error data; replace retry-as-approval with an explicit approval bound to a commit SHA and PR.
- Read a database-backed worker kill switch on every loop and verify graceful SIGTERM draining.
- Add chaos tests for worker termination during publication, database disconnects, and GitHub timeouts; prove no duplicate PRs.
- Complete a backup/restore drill and activate the required operational alerts.

**Score rule:** no score is increased by these progress notes. Reliability and main-branch security remain below their Phase 2 targets until the complete evidence listed in section 2 is linked.
