# Agent–Worker Runtime traceability matrix — 2026-11-15

- **Repository:** `StrategyG/RusaAi`
- **Baseline:** `main` at `1adac8d340e5870570910168b6626fe285dd2cf2`
- **Implementation branch:** `feat/agent-worker-runtime-hardening-20261115`
- **Status legend:** IMPLEMENTED = code path exists and has relevant test coverage; PARTIAL = some controls exist but an acceptance dependency remains; MISSING = no implementation found in the audited baseline/branch; NOT_VERIFIED = evidence is unavailable or has not run.

## Baseline and candidate evidence

- Mainline CI for baseline SHA `1adac8d340e5870570910168b6626fe285dd2cf2` completed successfully for the RUSA-AI CI and web CI workflows on 2026-11-13.
- PR #116 candidate head `faf686ac85875a930221ed92a357a9520a5947e5` has three successful checks: Test/lint/security, RAG/MCP integration, and production image build/scan. PR #116 is not merged and is not part of the mainline baseline.
- This implementation branch has not yet been certified by its exact-head CI at the time this matrix was authored.

## Traceability

| Requirement | Mainline baseline | Implementation branch | Evidence / remaining work |
|---|---|---|---|
| Durable task queue | IMPLEMENTED | IMPLEMENTED | Existing PostgreSQL `aicode_jobs`; no second queue introduced. |
| Task contract and lifecycle state machine | PARTIAL | PARTIAL | Mainline has queued/running/awaiting_approval/succeeded/failed/cancelled and lease identity. Versioned TaskPlan/DAG contract is only in unmerged PR #116. |
| Per-tenant bounded pending queue | MISSING | IMPLEMENTED — CI pending | `RUSA_MAX_PENDING_TASKS_PER_TENANT`, tenant advisory lock, 429 + Retry-After, concurrency test. |
| Global bounded active claims | MISSING | IMPLEMENTED — CI pending | `RUSA_MAX_ACTIVE_TASKS`, global advisory lock, multi-worker claim test. |
| Idempotent enqueue | IMPLEMENTED | IMPLEMENTED | Existing tenant/key/payload comparison retained; replay resolved before capacity check. |
| Lease expiry and queue-transition fencing | IMPLEMENTED | IMPLEMENTED | Existing lease UUID and token predicates; new `with_lease_owned` callback. |
| Stale worker memory/checkpoint writes | PARTIAL | IMPLEMENTED — CI pending | New `LeaseBoundMemory`; PostgreSQL integration test verifies stale token cannot persist memory. DAG checkpoint CAS remains in unmerged PR #116. |
| Stale worker workspace writes | PARTIAL | IMPLEMENTED — CI pending | Tool Gateway checks live lease; developer write uses queue row lock. Read-only tools use a live-lease pre-check. |
| Bounded retry and terminal failure | PARTIAL | PARTIAL | Attempts/max_attempts already exist; branch adds 1–10 validation, 0–3600s delay, retryable flag and transient-only runtime classification. Full error taxonomy/backoff policy remains limited. |
| Approval distinct from retry | PARTIAL | PARTIAL | Generic retry is failed-only. Mainline legacy path still lacks a fingerprint-bound approval-resume endpoint. |
| Task deadline | IMPLEMENTED | IMPLEMENTED | Existing `RUSA_TASK_TIMEOUT_SECONDS` and `asyncio.wait_for`; production timeout/soak still NOT_VERIFIED. |
| Cancellation releases sandbox resources | PARTIAL | PARTIAL | Branch propagates lease loss to sandbox container cleanup and shortens heartbeat interval. Non-sandbox external I/O is only bounded by its own timeout. |
| Tenant API authorization and repository scope | IMPLEMENTED | IMPLEMENTED | Authenticated tenant claims, repository allowlist, tenant predicates, RLS migrations and tests. Production-role adversarial verification NOT_VERIFIED. |
| Least-privilege tool permissions | IMPLEMENTED | IMPLEMENTED | Role allowlists, protected paths, fixed validation profiles, Docker no-network/read-only snapshot. Rootless production daemon verification NOT_VERIFIED. |
| Task policy audit | IMPLEMENTED | IMPLEMENTED | PostgreSQL task-policy events, RLS, append-only controls and startup checks. |
| Actual tool invocation audit | MISSING | IMPLEMENTED — CI pending | New HMAC `tool.intent` and `tool.outcome` records with metadata only; production audit-store failure blocks execution. Off-host retention NOT_VERIFIED. |
| Worker inference through gateway/admission | PARTIAL | NOT_VERIFIED | Provider and admission components exist, but every production worker configuration/credential route must be checked against the target deployment. RusaModels remains Model Plane. |
| Tenant/project-scoped RAG retrieval | IMPLEMENTED | IMPLEMENTED | Knowledge service receives tenant identity and server-derived project scope. Data-source rights/provenance and retrieval-quality evaluation remain separate gates. |
| Independent verification | PARTIAL | PARTIAL | CI runs tests, lint, Bandit, integration and image scans. No external security review, load/soak, or production operator sign-off. |
| Production readiness | NOT_VERIFIED | NOT_VERIFIED | No deployment, backup/restore, rollback, monitoring/alert drill, load/soak evidence, or operator sign-off. **NOT CERTIFIED.** |

## Exact branch test plan

The new tests added on this branch cover:

- PostgreSQL tenant capacity and per-tenant isolation.
- Idempotent replay at capacity.
- Concurrent enqueue race under the tenant advisory lock.
- Global active-task claim bound across tenants.
- Generic retry rejection for `awaiting_approval`.
- Non-retryable terminal failure followed by explicit retry.
- Stale lease callback rejection.
- Stale worker memory write rejection.
- Tool intent/outcome audit redaction and denial.
- Required audit failure blocks tool execution.
- Active sandbox container removal after cancellation.
- HTTP 429 and `Retry-After` on saturated task intake.

The workflow commands are documented in [implementation notes](../agents/AGENT_WORKER_RUNTIME_IMPLEMENTATION.md). A green CI run on another branch or commit is not evidence for this branch head.

## Blocking production gates

1. Exact-head CI green for all workflow jobs, including PostgreSQL race tests and sandbox image tests.
2. Production login-role/RLS/tenant-isolation adversarial test against the target database.
3. Live worker cancellation and lease-expiry drill, including Docker container and workspace cleanup.
4. Load/soak evidence at the configured active-task and inference-admission limits.
5. Backup/restore and image rollback drills with audit chain verification.
6. Off-host immutable audit retention and alert delivery test.
7. Security review and operator sign-off.

Until all seven gates have evidence, status remains **NOT CERTIFIED**.
