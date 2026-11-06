# RUSA-AI Production Readiness Evidence Ledger

## Scope

This ledger separates the API serving foundation from the complete RUSA-AI model and agent product. A successful build or unit-test run is not a production certification.

## Remediation delivered in this branch

- API-key authentication is mandatory on inference and task-validation endpoints. Missing or weak configuration fails closed.
- The mock completion was removed. Inference returns HTTP 503 until a local model is loaded.
- A local Transformers adapter uses on-disk model files only and disables remote custom code.
- A2A validation uses the schema-declared JSON Schema draft and a format checker.
- Missing or malformed schema configuration fails closed.
- Liveness and readiness checks are separated.
- Request IDs, basic security headers, a content-length limit, and generic internal error responses are added.
- Docker runtime uses the correct Python import path, non-root identity, and a liveness healthcheck.
- CI gates lint, compile, tests, dependency audit, image build, and a container liveness smoke test.
- The API database role is restricted to SELECT/INSERT on the task queue; it has no task mutation/deletion or memory-table/sequence privileges, and remains subject to forced RLS.
- Reusing a tenant idempotency key with a different normalized task payload returns HTTP 409; identical retries return the original task ID and work branch.
- The tenant-scoped agent API supports status, cancellation, and explicit retry of failed tasks; cancellation also supports approval-pending tasks and revokes the active worker lease.
- Cancellation/retry mutations execute through tenant-checked SECURITY DEFINER database functions; the API role has SELECT/INSERT and function EXECUTE only, with no direct UPDATE/DELETE table privileges.
- Compose runs a one-shot schema/privilege migration before the API and worker; API host ports remain loopback-bound by default.

## Current verification boundary

Latest CI on PR #15 commit d4c941e1aafcb7018df6bd277b01af2e03a8ebd4 passed both required jobs. PR #19 head 8a3dc95 also passed both CI jobs: lint, compile, unit/PostgreSQL integration tests, dependency audit, Compose validation, both image builds, agent import smoke test, and inference API liveness smoke test. These are code/build checks only, not production certification. The model adapter can be tested with a fake backend, but a real model cannot be tested without a compatible model artifact and target hardware.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| API foundation | CI, schema tests, auth tests, container smoke test | Passed on d4c941e; live deployment configuration and staging verification pending |
| Real model artifact | Model ID/revision, license, checksum, tokenizer, chat template | Blocked: no weights supplied |
| Model quality | Versioned benchmark dataset, task success, safety evaluation | Blocked |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency | Blocked |
| High-throughput serving | Runtime selection and load test for target model | Open |
| Multi-agent runtime | Hierarchical orchestrator, specialist workers, PostgreSQL queue, durable checkpoints, bounded retries, per-claim lease fencing | Implemented; PostgreSQL integration CI passed; staging soak and live multi-worker verification pending |
| Task lifecycle API | Tenant-scoped submit/status/cancel/retry, lease revocation, DB privilege boundary | CI passed on PR #19 head 8a3dc95; staging cancellation and lease-revocation verification pending |
| Episodic memory | PostgreSQL full-text retrieval, tenant RLS, TTL, redaction, deletion | Implemented; CI tenant-isolation/redaction tests passed; staging retention verification pending |
| Semantic RAG | Ingestion/chunking, embeddings, vector or hybrid retrieval, grounded citations, retrieval evaluation | Not implemented |
| MCP protocol | Authenticated MCP server/client, tenant-scoped tools/resources, protocol conformance tests | Not implemented; GitHub is accessed through a bounded native REST gateway |
| Security operations | TLS, gateway rate limiting, secret rotation, penetration tests | Deployment configuration required |
| Repository governance | Protected main branch, required CI checks, mandatory review | Blocked: GitHub currently reports main as unprotected; repository administrator action required |
| PR integration | Conflict-free integration into main and merged dependency chain | Blocked: PR #8 is dirty; PR #15 and PR #19 remain stacked; no production merge performed |
| Reliability operations | Backups, restore drill, rollback, alerting, incident runbook | Not implemented |
| Production rollout | Staging soak, canary, SLO approval, owner sign-off | Not performed |

## Queue lease fencing

Each task claim receives a cryptographically random lease token. Heartbeat,
checkpoint, acknowledgement and retry operations require both the worker ID
and the current token. Reclaiming a task rotates the token, so a stale execution
is rejected even if the same worker ID is reused. The token column is added
through an idempotent schema migration and is required by queue readiness.

The worker credential is an internal service identity and must never be exposed
to tenant clients. API database credentials remain NOBYPASSRLS. CI must pass the
PostgreSQL same-worker-ID stale-token test before release. The API role has no
direct UPDATE or DELETE access to queue tables. Tenant cancellation and failed-task
requeue are exposed only through SECURITY DEFINER functions that verify the
transaction-local tenant context and restrict eligible task states. Function
execution is revoked from PUBLIC by default and explicitly granted only to the tenant API and worker service roles for the two required lifecycle functions.

For rollout from a worker version that does not send lease tokens, drain and stop
all old worker replicas before enabling the new queue migration and starting the
new worker pool. Do not run mixed worker versions against the same queue: older
workers cannot honor token fencing.

## Deployment rules

- Do not merge or deploy while required CI checks are failing.
- Do not set readiness to healthy unless the model, API-key configuration, and task schema are present.
- Do not expose the API directly to the public internet. Use a TLS gateway with distributed rate limits, timeouts, and network controls.
- Do not commit model weights, secrets, or private datasets to Git.
- Do not call the full product production-ready until every applicable release gate has objective evidence and human approval.

## Distributed worker failure semantics

- Task submission requires a tenant-scoped `Idempotency-Key`. PostgreSQL enforces uniqueness on `(tenant_id, idempotency_key)`; an identical payload returns the original task ID, while a different payload returns HTTP 409.
- Claims use `FOR UPDATE SKIP LOCKED`. Each claim rotates a cryptographically random lease token. Heartbeat, checkpoint, retry, and acknowledgement are conditional writes that require the current worker ID, token, running state, and unexpired lease.
- A worker crash leaves the lease to expire. A later claim rotates the token; the old worker can no longer checkpoint, retry, or acknowledge, even if it reconnects using the same worker ID.
- Attempts are bounded by `max_attempts` (default 4, hard maximum 10). Expired work at the attempt limit becomes `failed`; explicit tenant retry resets the counter and clears the failed result.
- Cancellation and acknowledgement serialize through PostgreSQL row updates. If cancellation wins, it clears the lease and the stale acknowledgement is rejected. If success acknowledgement wins first, the terminal task cannot be cancelled or reopened by the old worker.

### External side effects and ambiguous outcomes

The queue provides at-least-once task execution, not exactly-once execution across PostgreSQL and GitHub. These systems do not share a transaction and the GitHub REST operations do not provide a general idempotency-key contract.

The GitHub adapter therefore reconciles uncertain outcomes before retrying:
- Branch creation uses a deterministic tenant/task branch; after an ambiguous create response it reads the ref and reuses it if present.
- File writes read the current work-branch file first, skip identical content, and after an ambiguous PUT response read it back. The operation is treated as successful only when the stored bytes match the requested content.
- Draft PR creation searches for an existing open PR for the exact head/base pair before creating one, and searches again after an ambiguous POST response.
- If the external service cannot be queried or the observed state does not match the intended operation, the tool reports failure/unknown outcome. It must not blindly repeat a non-idempotent side effect. The task is left for bounded retry/reconciliation or human review.

A crash can occur after GitHub commits a side effect but before the worker persists its checkpoint. Recovery may re-execute the tool call; the adapter's read-before-write and post-timeout reconciliation make supported GitHub operations converge. This is not a distributed exactly-once guarantee. Any future tool without a safe reconciliation key/read-after-write path must be marked non-retryable after an unknown outcome and routed to manual reconciliation.

### Required PostgreSQL regression coverage

The PostgreSQL suite includes duplicate enqueue/payload conflict, lease expiry after simulated worker death, same-ID stale-token rejection, concurrent competing claims, retry exhaustion, cancellation racing with success acknowledgement, and terminal-state non-resurrection. These tests require the CI PostgreSQL service; a skipped local test is not evidence of a pass.
