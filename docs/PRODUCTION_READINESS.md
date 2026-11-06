# RUSA-AI Production Readiness Evidence Ledger

## Scope

This ledger distinguishes the API serving foundation from the complete model and autonomous-agent product. A successful build or unit-test run is not a production certification.

## Evidence observed

- Commit `f81be2e`: CI lint, compile, unit tests, production dependency audit, Docker build, container startup, and liveness smoke test all passed.
- The subsequent GitHub gateway and sandbox additions are pending their own CI run and have not been exercised against a real repository token or a live rootless gVisor runtime.

## Remediation delivered

- API-key authentication is mandatory on inference and task-validation endpoints. Missing or weak configuration fails closed.
- Mock inference was removed. Inference returns HTTP 503 until a local model is loaded.
- Local Transformers loading uses on-disk files only and disables remote custom code.
- A2A validation uses the schema-declared JSON Schema draft and format checks.
- Liveness and readiness are separate.
- Request IDs, basic security headers, an ASGI request-body cap (including chunked bodies), model input-token/context limits, and generic internal error responses are added.
- Docker runtime uses the correct Python import path, non-root identity, and a liveness healthcheck.
- CI gates lint, compile, tests, dependency audit, image build, and container liveness.
- AICode contracts, fail-closed policy, tenant-scoped single-instance memory, HMAC audit, worker orchestration, evidence gate, GitHub gateway, and constrained Docker/gVisor sandbox adapters have been added.

## Release gates

| Gate | Evidence required | Status |
|---|---|---|
| API foundation | CI, schema/auth tests, body-size/context tests, container smoke test | Passed on commit f81be2e; current hardening changes pending CI |
| GitHub gateway | Mock API tests, token scope review, staging read/write/PR verification | Pending current CI and staging |
| Sandbox | Rootless daemon, gVisor runtime, pinned image, timeout/cleanup tests | Code added; live runtime not verified |
| Worker tool loop | Typed tool-call loop connected to gateway and sandbox | Not implemented |
| Durable orchestration | Queue, idempotency, retries, cancellation, recovery, dead-letter queue | Not implemented |
| Shared memory | PostgreSQL, migrations, tenant isolation, retention/deletion, backup/restore | Not implemented |
| Real model artifact | Model ID/revision, license, checksum, tokenizer, chat template | Blocked: no model weights supplied |
| Model quality | Versioned benchmark dataset, task success, safety evaluation | Blocked |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency | Blocked |
| Security operations | TLS, gateway rate limiting, secret rotation, penetration tests | Deployment configuration and testing required |
| Reliability operations | Backups, restore drill, rollback, alerting, incident runbook | Not implemented |
| Production rollout | Staging soak, canary, SLO approval, owner sign-off | Not performed |

## Deployment rules

- Do not merge or deploy while required CI checks are failing.
- Do not set readiness healthy unless the model, API-key configuration, and task schema are present.
- Do not expose the API directly to the public internet. Use a TLS gateway with distributed rate limits, timeouts, and network controls.
- Do not commit model weights, secrets, or private datasets to Git.
- Do not enable repository code execution without a dedicated ephemeral VM/runner and reviewed tool authorization.
- Do not call the complete product production-ready until every applicable release gate has objective evidence and human approval.
