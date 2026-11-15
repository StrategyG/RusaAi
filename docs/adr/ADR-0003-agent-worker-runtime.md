# ADR-0003: Staged Agent–Worker Runtime on the Existing Durable Queue

- **Status:** Accepted for staged implementation; activation remains blocked
- **Date:** 2026-11-15
- **Owners:** RusaAi Control Plane maintainers and project owner
- **Related:** ADR-0001 (RusaAi-Edge CPU-first baseline), ADR-0002 records (canonical knowledge store and Model Plane boundary), `docs/AGENT_RUNTIME_V1.md`

## Context

RusaAi already has a PostgreSQL-backed AICode queue, tenant RLS, lease tokens, heartbeat, retry/recovery, task memory, Tool Gateway, sandbox, policy audit and an authenticated inference gateway. A new Agent Runtime must not create a parallel queue, lease registry, model server or tool-execution authority.

The broad Agent/Worker role catalog is a proposal. Registry membership alone is not authorization. Production policy and independent verification adapters, role-specific handler approvals and the canonical source/role crosswalk are separate gates.

## Decision

1. Reuse `aicode_jobs` as the only durable task queue.
2. Validate a versioned TaskPlan/DAG before queue intake; derive tenant identity from the authenticated principal, never model output.
3. Execute nodes sequentially on the CPU-first profile, with bounded node count, deadline, retry limit and dependency checks.
4. Fence every queue transition and checkpoint write with the current job/task/tenant/worker/lease identity. Use monotonic checkpoint CAS to reject stale writers.
5. Keep human approval distinct from retry. Generic retry is valid only for `failed`; approval-pending nodes require explicit approval bound to the exact plan fingerprint and eligible node IDs.
6. Bound pending work per tenant with `RUSA_MAX_PENDING_TASKS_PER_TENANT` (default 1000), counting queued, running and approval-paused work. Bound active worker claims globally with `RUSA_MAX_PARALLEL_WORKERS` (default 1) using a transaction-scoped advisory lock.
7. Keep tool execution behind the existing role allowlists, Policy Engine, Tool Gateway and sandbox. Do not grant tools because a role appears in the registry.
8. Route all worker inference through the RusaAi Control Plane gateway/admission controller. RusaModels remains the Model Plane.
9. Record durable HMAC-chained node outcome audit events without raw model output, tool arguments or secrets.
10. Keep the registry draft/disabled until role owners, source/rights reviewers, security, QA and the project owner approve activation.

## Consequences

- The existing AICode six-worker pipeline remains compatible and remains the only default executable worker set.
- The Agent Runtime DAG path is staged and fail-closed. The 24 registry roles are not activated.
- Queue saturation returns HTTP 429 with a retry hint; idempotent replays are resolved before capacity checks.
- All worker replicas must share the same global concurrency setting.
- Actual per-tool invocation audit and external side-effect idempotency remain open implementation gaps.
- Production deployment is not certified by unit tests or CI alone.

## Migration and rollback

Apply migrations in order: 001–012, migration 013 from the canonical RAG governance sequence, 014 checkpoint CAS, then 015 task approval audit action. Do not apply 014/015 ahead of the reserved 013 migration.

Rollback should revert application images first and leave the additive 014 column and expanded 015 action constraint in place. Do not remove checkpoint data or audit events. Reversing 015 requires proof that no `task.approve` events exist and explicit DBA approval; dropping 014 requires a separate compatibility review and verified backup.

## Rejected alternatives

- A second queue, broker or lease registry: duplicates durable state and increases split-brain risk.
- Letting retry resume `awaiting_approval`: collapses execution retry and human authorization.
- Allowing registry membership to grant tools: confuses routing metadata with authority.
- Direct worker-to-RusaModels calls: bypasses tenant-aware RusaAi admission and audit.
- Activating all role profiles from configuration alone: no reviewed production policy/verifier or role handlers exist.
