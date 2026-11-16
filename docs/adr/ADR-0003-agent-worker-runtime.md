# ADR-0003 — Agent–Worker Runtime: bounded queue, lease fencing, and tool audit

- **Status:** Proposed; implementation candidate exists on `feat/agent-worker-runtime-hardening-20261115`, pending review and CI.
- **Date:** 2026-11-15
- **Decision owners:** RusaAi maintainers and production operator.
- **Baseline:** `main` at `1adac8d340e5870570910168b6626fe285dd2cf2`.

## Context

RusaAi is the Control Plane. RusaModels is the Model Plane and remains responsible for model artifacts, model residency, and inference execution. Worker model calls must use the configured RusaAi gateway/admission path; a worker must not load or serve model weights directly.

The default branch already has a durable PostgreSQL `aicode_jobs` queue, tenant-scoped task intake, lease tokens, idempotency keys, retry counters, tenant RLS, task-policy audit, an HMAC-chained audit ledger, a role allowlist, and a Docker sandbox. The existing implementation still has material gaps: no per-tenant pending-work cap, no cross-replica active-claim cap, legacy memory writes are not lease-fenced, Tool Gateway invocations are not individually audited, sandbox subprocesses do not receive the lease-revocation signal, and generic retry can requeue approval-paused tasks.

PR #116 (`feature/agent-runtime-v1`) is an open, unmerged Agent Runtime/DAG candidate. Its exact head `faf686ac85875a930221ed92a357a9520a5947e5` has successful CI checks, but those changes are not part of `main`. This ADR hardens the existing mainline AICode queue and deliberately does not create a second queue or claim that the DAG runtime is deployed.

## Decision

1. **Reuse `aicode_jobs` as the only durable task queue.** Do not add a parallel task registry, queue table, or scheduler database.
2. **Bound pending intake per tenant.** `RUSA_MAX_PENDING_TASKS_PER_TENANT` defaults to 1000 and accepts 1–1,000,000. Count `queued`, `running`, and `awaiting_approval` rows. Serialize the count-and-insert path with a transaction-scoped tenant advisory lock. Return HTTP 429 with `Retry-After` when full.
3. **Resolve idempotency before capacity.** An identical tenant/key/payload replay returns the original task ID and state even when the tenant is at capacity. A different payload for the same key remains a conflict.
4. **Bound active queue claims globally.** `RUSA_MAX_ACTIVE_TASKS` defaults to 1 and accepts 1–64. A transaction-scoped global advisory lock serializes active-lease counting and `SKIP LOCKED` claiming across worker replicas. This setting is distinct from `RUSA_MAX_PARALLEL_WORKERS`, which remains the in-task specialist concurrency limit.
5. **Fence short durable writes.** Queue-row identity (job, task, tenant, worker, lease token, running status, unexpired lease) must be verified under `FOR UPDATE` while legacy task-memory reads/writes and local workspace file writes run. Do not hold the queue row lock across model inference, Git/network requests, or long sandbox work.
6. **Check leases before read-only tools.** Tool Gateway read/list/diff/knowledge/check operations must verify the current lease before execution. Workspace writes use the lease-row lock. Role allowlists, path protections, sandbox profiles, and no-arbitrary-shell policy remain the final enforcement boundary.
7. **Audit every actual tool invocation.** Persist `tool.intent` before execution and `tool.outcome` after allow/deny/result through the existing HMAC-chained PostgreSQL audit ledger. Include actor, task, tenant, tool, repository/project scope, database timestamp, decision, and result status. Exclude raw arguments, file contents, model output, tool output, credentials, and lease tokens. Production fails closed if the required intent audit cannot be written.
8. **Make cancellation cooperative and bounded.** Default lease heartbeat interval is 10 seconds (configurable 1–60). Lease revocation cancels the orchestrator and is passed to DockerSandbox so its named container is force-removed. Other external I/O remains bounded by its own timeout.
9. **Separate retry from approval.** Maximum attempts are bounded to 1–10. Retry delay is bounded to 0–3600 seconds. The worker retries classified timeout/connection transport failures; deterministic policy/permission and blocked verification outcomes are terminal. Generic retry is allowed only for `failed`. Approval-paused work requires an explicit approval workflow and must not be silently converted to a retry.
10. **Keep model-plane ownership unchanged.** RusaAi routes inference through its gateway/admission controller; RusaModels owns model artifacts and inference runtime. No weights or secrets are committed.
11. **Do not add a schema migration for this hardening.** Existing queue, lease, idempotency, RLS, and audit tables are sufficient.

## Configuration

| Variable | Default | Valid range | Consumer |
|---|---:|---:|---|
| `RUSA_MAX_PENDING_TASKS_PER_TENANT` | 1000 | 1–1,000,000 | API replicas |
| `RUSA_MAX_ACTIVE_TASKS` | 1 | 1–64 | Worker replicas |
| `RUSA_LEASE_HEARTBEAT_INTERVAL_SECONDS` | 10 | 1–60 | Worker replicas |
| `RUSA_MAX_PARALLEL_WORKERS` | existing setting | 1–32 | In-task specialist concurrency |

All API replicas must use the same pending limit. All worker replicas must use the same active-task limit and heartbeat interval. Do not assume per-process settings create a global bound; the PostgreSQL advisory locks provide the cross-replica serialization.

## Consequences

- Queue saturation becomes explicit and retryable instead of unbounded growth.
- A tenant's approval-paused tasks consume its pending allowance and must be reviewed/cleared deliberately.
- Global active-task admission is serialized by one advisory lock; the default of one active task is conservative for CPU-first deployments.
- Stale workers cannot update the legacy task-memory checkpoint or write through the model-facing workspace tool after losing their lease.
- Tool invocation evidence becomes attributable without retaining secrets or untrusted tool payloads.
- The legacy mainline does not yet expose a plan-fingerprint-bound approval-resume endpoint. An `awaiting_approval` task is therefore an operator-handled lifecycle gap until the reviewed approval path is available.
- PR #116 remains a separate, unmerged Agent Runtime/DAG candidate. Its green CI is evidence for that PR head only, not evidence that mainline has the DAG runtime.

## Deployment and rollback

No database migration is required. Roll out the API and worker images as one coordinated change:

1. Keep `RUSA_AICODE_ENABLED=false`, stop intake, and drain/cancel active worker leases.
2. Verify a database backup and record the previous API/worker image digests.
3. Deploy the reviewed API image to every API replica with one shared pending limit.
4. Deploy the reviewed worker image to every worker replica with one shared active-task limit and heartbeat interval.
5. Run PostgreSQL queue/race tests, tool-audit tests, lease-loss tests, and sandbox-cancellation smoke tests.
6. Re-enable intake only after operator approval.

Rollback requires no schema rollback: disable intake, stop/drain workers, and deploy the previous API/worker image digests together. The previous images do not enforce these code-level bounds, so keep intake disabled until the rollback topology is stable. Never delete or rewrite audit records.

## Verification and production status

The implementation branch adds tests for tenant capacity, idempotent replay at capacity, concurrent enqueue races, global active claims, failed-only retry, non-retryable terminal failure, stale lease callbacks, stale memory writes, tool audit redaction, and sandbox cancellation. CI must run against the exact branch head before merge.

**Production readiness: NOT CERTIFIED.** CI does not establish deployment correctness. Certification still requires production-role adversarial tenant-isolation tests, load/soak tests, backup/restore and rollback drills, off-host immutable audit retention, monitoring/alert verification, and operator sign-off.
