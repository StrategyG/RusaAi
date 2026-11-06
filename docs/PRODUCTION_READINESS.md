# RUSA-AI Production Readiness Evidence Ledger

## Scope

RUSA-AI has three implementation layers: local-model inference API, hierarchical coding-agent runtime, and optional RAG/MCP knowledge plane. This branch is a substantially implemented software foundation, not a production certification or a trained model release. A green CI run proves only the checks executed against that exact commit.

## Implemented in this branch

- Authenticated inference and tenant-scoped agent task APIs with bounded request bodies, request IDs, generic error handling, and separate liveness/readiness checks.
- Local Transformers model adapter using on-disk artifacts and disabling remote custom code. Inference remains not-ready until a compatible model artifact is supplied and loaded; no mock completion is presented as real inference.
- A2A task-envelope schema validation with the declared JSON Schema draft and format checks.
- Hierarchical master orchestrator with six specialist workers: Analyst, Architect, Developer, Debugger, Tester, and Security.
- PostgreSQL task queue with tenant-scoped idempotency, durable checkpoints, expiring leases, heartbeat renewal, bounded retries, and episodic memory.
- Least-privilege GitHub gateway for allowlisted repositories, isolated work branches, protected path checks, draft pull requests, CI status, and complete branch-diff evidence. Arbitrary shell, unrestricted network, merge, deployment, release, secret changes, permission changes, and workflow changes are not exposed to workers.
- Independent tester and security verification stages, commit-SHA matching, required CI check validation, and bounded repair cycles.
- Optional local-document RAG ingestion (TXT, Markdown, HTML, PDF, DOCX), chunking, 1,536-dimensional embeddings, PostgreSQL/pgvector vector retrieval, PostgreSQL full-text retrieval, weighted hybrid ranking, source provenance, and offline retrieval metrics.
- Optional stateless MCP Streamable HTTP knowledge service with read-only tools, per-tenant bearer-token authentication, request-local tenant context, PostgreSQL RLS, and a restricted NOBYPASSRLS knowledge role.
- Non-root container images, read-only runtime filesystems where applicable, healthchecks, Compose deployment topology, CI lint/compile/test/dependency-audit gates, image builds, and container smoke tests.

## Verification boundary

The repository has automated unit, contract, security, and PostgreSQL/pgvector integration tests. CI must be green on the exact candidate commit, including dependency audit, Docker builds, and container smoke tests, before code is considered eligible for review. Do not treat a prior green run as evidence for a later commit.

The most recent CI run before the follow-up fixes reported 129 passed and 10 failed. The identified causes were psycopg bulk-insert API misuse and a scripted test worker missing the explicit tool-authorization contract. Follow-up commits address those defects; the latest CI run must confirm the result before this ledger is marked green.

## Release gates

| Gate | Evidence required | Status |
|---|---|---|
| Inference API foundation | Green CI, schema/auth tests, container smoke test | Pending exact-commit CI |
| Real model artifact | Model ID/revision, license, checksum, tokenizer, chat template, reproducible loading | Blocked: model weights not supplied |
| Model quality and safety | Versioned benchmark dataset, task success, safety and regression evaluation | Blocked |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency, target-hardware soak test | Blocked |
| High-throughput serving | Runtime selection and measured load test for the selected model | Open |
| Agent runtime | End-to-end staging tasks, GitHub permissions, queue recovery, lease fencing, CI evidence freshness | Implemented; staging verification pending |
| Memory/RAG/MCP | Tenant-separated gold set, Recall@K/MRR/nDCG, citation accuracy, prompt-injection and cross-tenant tests | Implemented; domain evaluation and staging verification pending |
| Security operations | TLS ingress, distributed rate limiting, egress allowlists, secret rotation, external penetration test | Partial; deployment controls required |
| Reliability operations | PostgreSQL backup/PITR, restore drill, immutable remote audit, monitoring, alerting, incident runbook, rollback rehearsal | Partial; operational drills pending |
| Production rollout | Staging soak, canary, SLO approval, owner sign-off | Not performed |

## Mandatory deployment checklist

- [ ] Supply and verify a licensed, versioned model artifact and tokenizer.
- [ ] Run model quality, safety, and target-hardware performance benchmarks.
- [ ] Use a TLS gateway with distributed rate limits, request timeouts, and network/egress controls.
- [ ] Store secrets in a secret manager; test tenant credential rotation and revocation.
- [ ] Use separate PostgreSQL owner, worker, API, and restricted knowledge roles; verify RLS using the actual runtime identities.
- [ ] Configure encryption, backups, point-in-time recovery, and complete a restore drill.
- [ ] Ship HMAC audit records to immutable remote retention and test key rotation.
- [ ] Complete prompt-injection, tool-abuse, cross-tenant, worker-crash, duplicate-delivery, and lease-expiry tests.
- [ ] Pin dependencies and container images; generate an SBOM and artifact provenance.
- [ ] Complete staging soak, monitoring/alerting, incident response, rollback rehearsal, and human approval.

## Deployment rules

- Do not merge or deploy while required CI checks are failing or pending.
- Do not set readiness to healthy unless the required model artifact and API configuration are present.
- Do not expose the API or plain-HTTP MCP endpoint directly to the public internet.
- Do not commit model weights, secrets, or private datasets to Git.
- Keep merge, deployment, release, secret changes, permission changes, and workflow changes behind human approval and protected GitHub controls.
- Do not call the complete product production-ready until every applicable release gate has objective evidence and human sign-off.
