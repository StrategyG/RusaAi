# ADR-0002 — Bounded Agent/Worker Admission

- **Status:** Proposed for review
- **Date:** 2026-11-15
- **Scope:** RusaAi Control Plane task queue and worker admission
- **Baseline:** `main` at `1adac8d340e5870570910168b6626fe285dd2cf2`

## Context

The existing `PostgresTaskQueue` is durable and tenant-aware, and its lease
transitions are fenced by worker identity and lease token. However, the
mainline implementation did not enforce a per-tenant pending-task ceiling or
a database-coordinated global active-worker ceiling. The runtime composition
also defaulted to four parallel specialist workers while the CPU operations
profile documented one.

The separate Agent Runtime candidate is PR #116. This ADR must not create a
second queue, registry, DAG executor, lease system, checkpoint store, or
orchestrator. It hardens the existing PostgreSQL queue and preserves the
Control Plane / Model Plane boundary.

## Decision

1. **Reuse the existing PostgreSQL queue.** No broker, second queue, new
   scheduler service, or queue table is introduced.
2. **Bound tenant backlog.** Enqueue operations take a transaction-scoped
   advisory lock derived from the tenant ID, then count that tenant's
   `queued`, `running`, and `awaiting_approval` jobs before inserting.
   The default ceiling is 100 pending tasks per tenant, configurable with
   `RUSA_MAX_PENDING_TASKS_PER_TENANT` (1–10,000).
3. **Preserve idempotency under saturation.** An existing idempotency key with
   an identical normalized payload returns its original task ID and state
   before the capacity check. Reusing the key with a different payload
   remains a conflict. A duplicate task ID without the matching key is not
   treated as a new task.
4. **Bound active workers across replicas.** Every new worker claim takes the
   same transaction-scoped PostgreSQL advisory lock, counts all live
   `running` leases, and claims only when the configured limit is not reached.
   The default is one active task across the worker fleet; configure
   `RUSA_WORKER_MAX_ACTIVE_JOBS` to a value from 1–64 only after capacity
   evidence exists.
5. **Bound retries.** A task may request 1–10 attempts (default 3). Retry
   delay is clamped to 0–3,600 seconds. Explicit operator/tenant-authorized
   retry remains a separate audited action and starts a new bounded attempt
   cycle.
6. **Align CPU worker parallelism.** The runtime default for
   `RUSA_MAX_PARALLEL_WORKERS` is one, matching the documented CPU profile.
   Raising it requires representative memory, latency, and concurrency tests.
7. **Keep inference admission separate.** Worker inference continues through
   the RusaAi gateway/admission path to RusaModels. This task-queue limit is
   not a model-generation semaphore and does not move model artifacts or
   inference runtimes into RusaAi.
8. **Avoid a database migration.** The change uses existing queue columns,
   indexes, RLS policies, and PostgreSQL advisory locks. No data rewrite or
   migration is required.

## Security and consistency constraints

- API admission sets the authenticated tenant context before locking or
  counting. The API role remains restricted by tenant RLS.
- The global live-lease aggregate is executed only by the trusted worker
  runtime role, whose existing queue policy permits cross-tenant scheduling.
  The aggregate reads counts only; it does not expose task payloads.
- Queue claim and enqueue operations fail closed if the advisory lock or
  capacity query fails.
- All API replicas must use the same
  `RUSA_MAX_PENDING_TASKS_PER_TENANT` value. All worker replicas must use the
  same `RUSA_WORKER_MAX_ACTIVE_JOBS` value.
- Old application replicas do not take these advisory locks. Drain old API
  and worker processes before enabling the new limit; otherwise old code can
  bypass the new admission boundary.
- Advisory-lock capacity does not provide exactly-once external side effects.
  Handlers still need downstream idempotency keys and read-after-write
  reconciliation across crash/retry boundaries.

## Rollout and rollback

No schema migration is involved. Roll out in a maintenance window:

1. Stop new task intake and drain all worker processes running old code.
2. Deploy the same reviewed RusaAi commit to every API and worker replica.
3. Set identical capacity variables on all replicas.
4. Restart workers, verify queue health and capacity behavior, then reopen
   intake.
5. Observe saturation, task age, lease expiry, and worker utilization before
   changing any limit.

Rollback requires stopping intake, draining new workers, restoring the prior
application image, and removing or reverting the new environment settings.
There is no database rollback because no schema or stored data is changed.
The prior application does not enforce the new capacity limits.

## Consequences

- Tenant backlog and active worker concurrency become bounded at the
  PostgreSQL transaction boundary for replicas running this implementation.
- The default CPU profile is conservative and serializes active task jobs.
- Capacity configuration drift across replicas can weaken the bound and is an
  operational deployment error.
- Production readiness still requires concurrency/load/soak tests, restart
  recovery, backup/restore, rollback drills, monitoring, security review, and
  operator sign-off. This ADR is not a production certification.
