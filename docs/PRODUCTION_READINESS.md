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

## Current verification boundary

The code changes require successful CI on the remediation pull request. The model adapter can be tested with a fake backend, but a real model cannot be tested without a compatible model artifact and target hardware.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| API foundation | CI, schema tests, auth tests, container smoke test | Pending CI on remediation PR |
| Real model artifact | Model ID/revision, license, checksum, tokenizer, chat template | Blocked: no weights supplied |
| Model quality | Versioned benchmark dataset, task success, safety evaluation | Blocked |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency | Blocked |
| High-throughput serving | Runtime selection and load test for target model | Open |
| Multi-agent runtime | Hierarchical orchestrator, specialist workers, PostgreSQL queue, durable checkpoints, bounded retries, per-claim lease fencing | Implemented in code; PostgreSQL integration CI and staging verification pending |
| Episodic memory | PostgreSQL full-text retrieval, tenant RLS, TTL, redaction, deletion | Implemented; staging retention and isolation verification pending |
| Semantic RAG | Ingestion/chunking, embeddings, vector or hybrid retrieval, grounded citations, retrieval evaluation | Not implemented |
| MCP protocol | Authenticated MCP server/client, tenant-scoped tools/resources, protocol conformance tests | Not implemented; GitHub is accessed through a bounded native REST gateway |
| Security operations | TLS, gateway rate limiting, secret rotation, penetration tests | Deployment configuration required |
| Repository governance | Protected main branch, required CI checks, mandatory review | Blocked: GitHub currently reports main as unprotected; repository administrator action required |
| PR integration | Conflict-free integration into main and merged dependency chain | Blocked: PR #8 is dirty and PR #15 remains stacked; no production merge performed |
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