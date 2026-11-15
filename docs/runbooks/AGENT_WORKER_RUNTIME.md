# Runbook: Agent–Worker Runtime

**Operational status:** staged / disabled by default. **Production certification:** NOT CERTIFIED.

## 1. Boundary and activation

- RusaAi is the Control Plane for tenant identity, task lifecycle, queue/lease, policy, Tool Gateway, audit and inference admission.
- RusaModels is the Model Plane for model artifacts, embeddings, inference runtime and Model Server.
- Worker inference must use the RusaAi authenticated gateway. Never inject the RusaModels API secret into a worker container.
- The checked-in registry is `draft_proposed_not_activated`. Preview endpoints are non-dispatching. Do not activate roles until role owners, source crosswalk, policy/verifier adapters and project-owner approval are recorded.

## 2. Resource settings

| Setting | Default | Meaning |
|---|---:|---|
| `RUSA_MAX_PENDING_TASKS_PER_TENANT` | 1000 | Maximum queued + running + awaiting-approval tasks per tenant |
| `RUSA_MAX_PARALLEL_WORKERS` | 1 | Global active, unexpired worker leases across replicas |
| Queue max attempts | 3 | Maximum queue attempts; accepted range 1–10 |
| Agent DAG worker parallelism | 1 | Sequential node execution in v1 |
| Inference active slots | 1 | Controlled by the separate durable inference admission controller |

All worker replicas must use the same `RUSA_MAX_PARALLEL_WORKERS` value. Increasing it requires capacity/load evidence and matching CPU/memory budgets. Queue capacity is a tenant backlog limit, not a throughput target.

## 3. Normal task flow

1. Authenticate API key; derive tenant from the principal.
2. Check repository allowlist and task permissions.
3. Validate the versioned plan, DAG dependencies, node count, deadlines, retry limits, role/scope and tool/permission requests.
4. Resolve idempotency key before applying tenant capacity.
5. Enqueue to PostgreSQL. At capacity, API returns HTTP 429 and `Retry-After: 30`.
6. Worker claims with `FOR UPDATE SKIP LOCKED`, a unique lease token and the global active-worker admission lock.
7. Worker verifies lease and task execution authorization, then executes one node at a time.
8. Policy is checked before worker execution; high/critical and production nodes require explicit approval.
9. Checkpoints use lease-bound I/O and CAS. Independent verification is required before a node is completed.
10. Queue completion/failure/approval is accepted only while the same lease is valid.

## 4. Approval and retry

- `awaiting_approval` is not a retryable failure.
- Use `POST /v1/agent-runtime/tasks/{task_id}/approve` with the exact plan fingerprint, eligible node IDs and a rationale, under `task:approve`.
- The queue rejects stale fingerprints, ineligible nodes and cross-tenant requests.
- `POST /v1/aicode/tasks/{task_id}/retry` is only for `failed` tasks. It must not be used to bypass approval.
- Retry attempts are bounded; external side-effect idempotency must still be proven by each handler.

## 5. Cancellation and stale workers

- Cancel via the tenant-authorized task cancellation endpoint.
- The worker heartbeat propagates lease loss/cancellation to the DAG executor.
- The executor cancels in-flight async work and does not write checkpoints after lease loss.
- A reclaimed job receives a new lease token. The old worker must not publish, complete or write a checkpoint.
- If a task remains running after a worker crash, inspect lease expiry and attempts. Do not manually edit lease tokens or queue rows to force progress.

## 6. Queue saturation

Symptoms: task intake returns 429 with a `Retry-After` header.

1. Check tenant pending count and task states using tenant-scoped task list/read APIs.
2. Identify stuck running leases, approval-paused tasks and retry storms.
3. Resolve failed tasks through the approved retry path; resolve approval-pending tasks through the explicit approval endpoint.
4. Tune the cap only after measuring database, worker and inference capacity. Apply the same setting to API and worker deployments.
5. Do not increase worker concurrency to compensate for a saturated Model Plane; the RusaAi inference admission controller remains authoritative.

## 7. Migration sequence

No live migration is authorized by this runbook.

1. Verify backup and restore evidence.
2. Verify baseline migrations 001–012.
3. Apply migration 013 from the RAG governance sequence (PR #115) after it is merged and approved.
4. Drain workers and apply migration 014 (checkpoint CAS).
5. Apply migration 015 (adds `task.approve` to the append-only policy audit action constraint).
6. Verify the `checkpoint_version` column, constraint definition, RLS, DB roles and audit triggers.
7. Deploy API and worker from the same reviewed source to staging; run tenant-isolation, lease-race, cancellation, approval and gateway tests.
8. Keep registry activation disabled pending separate approval.

## 8. Rollback

1. Stop/drain Agent Runtime workers.
2. Revert API and worker images together to the last compatible release.
3. Leave migration 014's additive `checkpoint_version` column in place; old code can ignore it.
4. Leave migration 015's expanded action constraint in place. If `task.approve` events exist, do not restore the older constraint.
5. Never delete checkpoint rows, truncate audit events, or reset lease tokens as part of rollback.
6. Any schema reversal requires a separately reviewed migration, verified backup and DBA approval.

## 9. Security and incident rules

- Never log API keys, bearer tokens, model prompts, raw model outputs or tool arguments.
- Use tenant/project-scoped retrieval and the approved worker repository allowlist.
- Keep Tool Gateway allowlists and sandbox network/filesystem boundaries authoritative.
- Missing policy/verifier adapters, invalid registry, failed audit write, expired lease or unknown worker identity must fail closed.
- Do not deploy, run destructive migrations, change permissions, activate roles or merge without explicit approval.

## 10. Evidence required before production

- Exact-head CI and full regression evidence.
- Adversarial tenant isolation and sandbox testing.
- Load/soak and multi-worker crash/restart tests.
- Backup/restore and rollback drills.
- Production TLS, secret-manager and Model Plane identity evidence.
- Monitoring, alerting and SLO ownership.
- Residual vulnerability disposition.
- Named operator and project-owner sign-off.
