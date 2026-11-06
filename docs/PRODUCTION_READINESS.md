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
- Multi-tenant repository allowlists and task-scoped work branches constrain GitHub write operations.
- PostgreSQL queue claims use random lease tokens; heartbeat, checkpoint, retry and acknowledgement are fenced by the active token.
- Orchestration stage results are persisted durably and resumed from the latest accepted checkpoint.
- Idempotency-key reuse is accepted only when the normalized task payload matches the original request.
- Queue readiness verifies forced row-level security, the tenant isolation policy and the lease-token column.

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
| Multi-agent runtime | Hierarchical orchestrator, workers, PostgreSQL leases/checkpoints, retries and idempotency | Implemented; integration CI and staging recovery tests pending |
| Memory/RAG | Storage, retrieval evaluation, tenant isolation, retention/deletion | Not implemented |
| Security operations | TLS, gateway rate limiting, secret rotation, penetration tests | Deployment configuration required |
| Reliability operations | Backups, restore drill, rollback, alerting, incident runbook | Not implemented |
| Production rollout | Staging soak, canary, SLO approval, owner sign-off | Not performed |

## Remaining runtime verification

- Run PostgreSQL integration tests with the least-privilege API and worker roles; skipped database tests are not passing evidence.
- Exercise expired lease takeover, stale-token rejection, worker crash/restart and concurrent claim races.
- Verify row-level security using non-owner, non-BYPASSRLS roles.
- Run request-size and timeout tests through the deployed reverse proxy.
- Complete backup/restore, load/soak, alerting, rollback and incident-response drills before production promotion.

## Deployment rules

- Do not merge or deploy while required CI checks are failing.
- Do not set readiness to healthy unless the model, API-key configuration, and task schema are present.
- Do not expose the API directly to the public internet. Use a TLS gateway with distributed rate limits, timeouts, and network controls.
- Do not commit model weights, secrets, or private datasets to Git.
- Do not call the full product production-ready until every applicable release gate has objective evidence and human approval.