# Agent/Worker Runtime — Operations Runbook

## 1. Scope and status

This runbook describes the bounded-admission hardening of the existing
RusaAi PostgreSQL task queue. RusaAi remains the **Control Plane**. RusaModels
remains the **Model Plane** and owns model artifacts, model residency,
llama.cpp, embeddings, and the Model Server.

The general Agent/Worker DAG runtime is a separate staged candidate in
[PR #116](https://github.com/StrategyG/RusaAi/pull/116). Its registry remains
draft/disabled until its production policy/verifier adapters, role handlers,
human-approval workflow, source crosswalk, and owner/security approvals are
complete. This runbook does not activate that registry.

## 2. Admission limits

The following settings are read by the existing `PostgresTaskQueue`:

| Setting | Default | Allowed range | Meaning |
|---|---:|---:|---|
| `RUSA_MAX_PENDING_TASKS_PER_TENANT` | 100 | 1–10,000 | Maximum tenant jobs in `queued`, `running`, or `awaiting_approval` |
| `RUSA_WORKER_MAX_ACTIVE_JOBS` | 1 | 1–64 | Maximum live `running` jobs across the worker fleet |
| `RUSA_MAX_PARALLEL_WORKERS` | 1 | 1–32 (orchestrator validation) | Specialist worker parallelism within one AICode task |
| `max_attempts` | 3 | 1–10 | Per-task execution attempts |
| retry delay | 30 seconds | 0–3,600 seconds | Delay before a failed task becomes claimable again |

Keep the two queue environment values identical across every API replica and
worker replica. A mismatch can weaken the bound: an API replica with a larger
pending limit can admit beyond a smaller replica's limit, and a worker replica
with a larger active-job limit can raise the effective fleet limit.

The CPU-first default is deliberately conservative: one active task and one
specialist worker at a time. Do not increase either value based only on a
green CI run. Use measured memory, queue latency, task duration, and model
server capacity on the target hardware.

## 3. Runtime behavior

### Tenant backlog

- Enqueue sets the authenticated tenant context and obtains a tenant-specific
  transaction advisory lock.
- The queue counts only that tenant's `queued`, `running`, and
  `awaiting_approval` rows.
- A new task is rejected when the count reaches the configured ceiling.
  The API returns HTTP 429 with `Retry-After: 5`.
- A repeated idempotency key with the same normalized payload returns the
  original task ID and status even when the tenant queue is full.
- A repeated key with a different payload remains a conflict (HTTP 409).
- Completed, failed, and cancelled jobs do not consume pending capacity.

### Worker admission and lease recovery

- Every worker claim obtains one global transaction advisory lock before
  counting live running leases and attempting a claim.
- The live count includes only `status='running'` rows whose lease has not
  expired. An expired lease does not consume an active slot; the existing
  claim query may reclaim it with a new unpredictable lease token.
- If the active limit is reached, `claim()` returns no job. The worker remains
  in its normal poll loop.
- Queue state transitions still require the current tenant, worker ID, lease
  token, status, and unexpired lease. Do not weaken these predicates.
- The global aggregate relies on the existing trusted worker RLS policy for
  cross-tenant scheduling. Do not grant that policy to API users or tool
  workers.

### Retry semantics

- Attempts are bounded to 1–10 per enqueue request.
- A failure delay above one hour is capped at one hour; negative values retain
  the prior immediate-retry behavior by clamping to zero.
- An explicit authorized retry is a new operator-controlled retry cycle.
  Retry is not an approval and does not satisfy any policy or human-review
  gate.
- Queue retry bounds do not make an external Git, deployment, or tool side
  effect exactly once. Handlers must use downstream idempotency keys and
  reconcile ambiguous outcomes before replaying side effects.

## 4. Deployment procedure — no migration

This change adds no database table or column and requires no schema migration.

1. Stop new task intake at the API edge.
2. Drain and stop **all old worker processes**. Old worker binaries do not
   take the new global claim lock and can bypass the active-job ceiling.
3. Drain or stop **all old API replicas**. Old API binaries do not take the
   tenant enqueue lock and can bypass the pending-task ceiling.
4. Deploy the same reviewed commit to every API and worker replica.
5. Set the same values for `RUSA_MAX_PENDING_TASKS_PER_TENANT` and
   `RUSA_WORKER_MAX_ACTIVE_JOBS` across the fleet. Keep the CPU defaults at
   100 pending per tenant, one active task, and one specialist worker unless
   an approved capacity test supports a different value.
6. Start the worker fleet and verify that no more than the configured number
   of live tasks are running.
7. Reopen task intake and monitor saturation, oldest pending task age,
   lease-expiry recovery, cancellation, and retry behavior.

Do not apply or invent a migration for this change. Existing RLS and queue
migrations remain authoritative.

## 5. Rollback

1. Stop new task intake.
2. Drain and stop every worker running the new code.
3. Restore the previous API/worker image as one coordinated release.
4. Remove or revert the new queue environment variables.
5. Verify the existing queue can claim and complete tasks before reopening
   intake.

There is no database rollback. The previous binary does not enforce these
limits, so rollback must not leave a mixed old/new worker fleet operating
concurrently.

## 6. Security and evidence requirements

- Keep API and worker database logins separate. The API role remains tenant
  RLS-scoped; only the trusted worker queue role may perform cross-tenant
  scheduling aggregates.
- Keep tool execution behind the existing role allowlists, Policy Engine,
  Tool Gateway, and isolated sandbox.
- Worker inference must use the RusaAi API/model-access gateway and its
  tenant-scoped admission credentials. Never expose the RusaModels inference
  token or direct Model Server/llama.cpp endpoint to workers.
- Audit events must not contain raw API keys, bearer tokens, database URLs,
  model weights, or secret values.
- Validate saturation and concurrency with the PostgreSQL race tests in
  `tests/test_queue_races.py`, plus the queue unit tests in
  `tests/test_queue_capacity.py`.
- Production qualification still requires staging load/soak, worker restart
  and lease-recovery drills, adversarial tenant-isolation tests, sandbox
  review, backup/restore, rollback, monitoring/alerting, vulnerability
  disposition, and operator sign-off.

## 7. Known gaps

- The Agent Runtime PR #116 is still a draft candidate, not mainline runtime.
- Handler-side idempotency and read-after-write reconciliation for every
  external side effect remain open.
- Production Policy Engine and independent verifier adapters require
  independent review.
- Durable human approval identity, separation of duties, and approval
  provenance require completion before activation.
- Staging/load/soak, backup/restore, rollback, monitoring, and operator
  sign-off are not evidenced by repository CI.
- The RusaAi-Edge checkpoint/tokenizer/model artifact remains a separate
  Model Plane gate and is not established by this work.
