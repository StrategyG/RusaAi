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
- The tenant API database role receives only `SELECT`, `INSERT`, and `UPDATE` on the task queue; it has no `DELETE` or memory-table/sequence privileges, broad table/sequence defaults are revoked, and it remains subject to forced RLS.
- Reusing a tenant idempotency key with a different normalized task payload returns HTTP 409; identical retries return the original task ID and work branch.
- The tenant-scoped agent API supports task status, cancellation, and explicit retry of failed tasks; cancellation revokes the active worker lease and is cooperative for external requests already in flight.
- Compose publishes API ports on host loopback by default and runs a one-shot schema bootstrap before the API and worker.

## Current verification boundary

Latest CI on PR #15 commit d4c941e passed both required jobs: lint, compile, 123 tests (including PostgreSQL integration), dependency audit, Docker Compose validation, both image builds, agent import smoke test, and inference API liveness smoke test. The task-lifecycle and database-permission remediation passed GitHub Actions on PR #16 head 8dc674f (run 42192747738): 135 tests passed, Ruff and compile passed, both dependency audits reported no known vulnerabilities, Compose validation passed, both images built, agent runtime imports passed, and inference API liveness passed. This run includes the regression check that worker startup preserves the API task-lifecycle grants. The current PR #16 head bf0338b also has successful required checks. This is code/container CI evidence only, not production certification. The model adapter can be tested with a fake backend, but a real model cannot be tested without a compatible model artifact and target hardware.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| API foundation | CI, schema tests, auth tests, container smoke test | PR #15 and PR #16 required CI checks passed; live deployment configuration and staging verification pending |
| Real model artifact | Model ID/revision, license, checksum, tokenizer, chat template | Blocked: no weights supplied |
| Model quality | Versioned benchmark dataset, task success, safety evaluation | Blocked |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency | Blocked |
| High-throughput serving | Runtime selection and load test for target model | Open |
| Multi-agent runtime | Hierarchical orchestrator, specialist workers, PostgreSQL queue, durable checkpoints, bounded retries, per-claim lease fencing | PR #16 CI passed with 135 tests, including PostgreSQL tenant/RLS, lease-fencing, task-lifecycle, least-privilege API DB tests, worker-restart grant preservation, and both container builds; staging verification pending |
| Episodic memory | PostgreSQL full-text retrieval, tenant RLS, TTL, redaction, deletion | Implemented; CI tenant-isolation/redaction tests passed; staging retention verification pending |
| Semantic RAG | Ingestion/chunking, embeddings, vector or hybrid retrieval, grounded citations, retrieval evaluation | Not implemented |
| MCP protocol | Authenticated MCP server/client, tenant-scoped tools/resources, protocol conformance tests | Not implemented; GitHub is accessed through a bounded native REST gateway |
| Task lifecycle API | Tenant-scoped status, cancellation, retry, idempotency, and lease revocation | Implemented in PR #16; CI passed on current draft head; staging verification pending |
| Security operations | TLS, gateway rate limiting, secret rotation, penetration tests | Deployment configuration required |
| Repository governance | Protected main branch, required CI checks, mandatory review | Blocked: GitHub currently reports main as unprotected; repository administrator action required |
| PR integration | Conflict-free integration into main and merged dependency chain | Blocked: PR #8 remains dirty; PR #15/16 stacked integration pending; no production merge performed |
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
PostgreSQL same-worker-ID stale-token test before release.
Tenant cancellation clears the active lease token. The worker rechecks ownership immediately before each repository tool action and fails closed when the lease is revoked or the database check fails. Cancellation is cooperative: an external request already in flight may finish, but the worker cannot start another tool or acknowledge success. Explicit API retry is restricted to failed tasks and does not requeue policy-blocked tasks.

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