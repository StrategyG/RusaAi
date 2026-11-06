# RUSA-AI Production Readiness Evidence Ledger

## Scope

This ledger distinguishes implemented repository foundations from evidence required to operate the complete RUSA-AI model and agent platform in production. A green CI run is necessary but is not a production certification.

## Implemented in the current development branch

- Authenticated API inference and A2A schema validation; missing/weak API-key configuration fails closed.
- Local-only Transformers adapter with no implicit model downloads or remote custom code.
- Liveness/readiness separation, request IDs, security headers, and request-size guard.
- Hierarchical AICode workers: analyst, architect, developer, debugger, tester, and security.
- Role-scoped GitHub tool gateway with feature-branch-only writes, protected paths, bounded tool calls, and draft PR creation.
- PostgreSQL durable queue with idempotency keys, leases, bounded retries, and worker process.
- Tenant-scoped PostgreSQL task memory and append-only hash-chained audit log; SQLite/JSONL remain development adapters.
- API-key hash mapping for authenticated tenant identity and per-tenant GitHub repository allowlists.
- Authenticated A2A task submission endpoint using Idempotency-Key.
- CI for lint, compile, tests, Bandit, dependency audit, container build, and API smoke test.

## Current verification

The AICode branch CI must pass after the latest changes. The runtime has not yet been validated against a live GitHub App installation, a real approved model provider, or a multi-worker PostgreSQL deployment. Multi-tenant API keys require a matching RUSA_GITHUB_TENANT_REPOSITORIES_JSON entry for every tenant; this is enforced at worker startup. The API model adapter has not been benchmarked with real model weights and target hardware.

## Release gates

| Gate | Required evidence | Status |
|---|---|---|
| Agent CI | Lint, compile, unit/integration tests, Bandit, dependency audit | Pending current branch CI |
| Container runtime | API and worker image build, startup, health and queue smoke tests | Pending current branch CI and runtime validation |
| PostgreSQL operations | Migration strategy, HA, connection pool, backup and restore drill | Open |
| Queue reliability | Lease heartbeat, current-owner fencing, tenant-scoped cancellation, dead-letter inspection, replay controls, queue-age alerts | Core lease/cancellation controls implemented; dead-letter operations and queue-age alerts remain open |
| Tenant security | Authenticated API-key hash mapping, per-tenant repository allowlists, and cross-tenant queue/memory/audit isolation tests | Implemented in code; production configuration and penetration validation required |
| GitHub permissions | GitHub App or fine-grained token, least privilege, rotation, installation scope tests | Deployment configuration required |
| Agent safety | Prompt-injection, malicious repository content, tool abuse, path and protected-file tests | Open |
| Provider security | HTTPS, explicit hostname allowlist, disabled ambient proxy inheritance, secret manager, egress restrictions | Host allowlist enforced by runtime; secret manager and network egress controls remain deployment requirements |
| Model artifact | License, immutable revision, checksum, tokenizer/chat template, provenance | Blocked: no model artifact supplied |
| Model quality | Versioned benchmark dataset, task success, safety and regression evaluation | Blocked |
| Hardware capacity | p50/p95 latency, throughput, concurrency, RAM/VRAM and context limits | Blocked |
| Observability | Central logs, metrics, traces, alerts, kill switch and incident runbook | Open |
| Resilience | Load/soak, failover, rollback, backup/restore and disaster recovery drills | Open |
| Production rollout | Staging canary, SLO approval, security review and owner sign-off | Not performed |

## Deployment rules

- Do not merge or deploy while required CI checks are failing or stale.
- AICode must only use least-privilege repository credentials and must not receive production secrets.
- Do not expose the API directly to the public internet; use a TLS gateway with distributed rate limits, request limits, timeouts, and network controls.
- Keep task worker egress restricted to the approved model provider and GitHub API.
- Do not treat SQLite memory or local JSONL audit as multi-replica production storage.
- Do not mark the complete RUSA-AI product production-ready until all applicable release gates have objective evidence and owner approval.


## Queue reliability implementation

The worker renews active PostgreSQL leases, and queue completion/failure writes require a current unexpired lease. Tenant-scoped cancellation immediately fences the current lease; the worker observes lease loss within at most 30 seconds and cancels orchestration. Expired final-attempt jobs are terminalized rather than stranded. PostgreSQL integration tests and operational validation remain required. Dead-letter inspection, operator replay, queue-age monitoring, and backup/restore remain open.
