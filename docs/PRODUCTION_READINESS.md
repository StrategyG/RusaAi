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
- Optional stateless MCP Streamable HTTP knowledge service with read-only tools, per-tenant bearer-token authentication, request-local tenant context, PostgreSQL RLS, and a restricted NOBYPASSRLS knowledge role. The worker membership uses INHERIT FALSE / SET TRUE so RAG table grants are available only after an explicit role switch.
- Non-root container images, read-only runtime filesystems where applicable, healthchecks, Compose deployment topology, CI lint/compile/test/dependency-audit gates, image builds, and container smoke tests.

## Verification boundary

The repository has automated unit, contract, security, and PostgreSQL/pgvector integration tests. CI must be green on the exact candidate commit, including dependency audit, Docker builds, and container smoke tests, before code is considered eligible for review. Do not treat a prior green run as evidence for a later commit.

The latest completed pull-request CI run validated code commit `932a840b64abd84c395836f3be5c14e7e68cc9ec` (run [42196444175](https://github.com/StrategyG/RusaAi/actions/runs/42196444175)):

- Ruff lint and Python compile: passed.
- Pytest with PostgreSQL 16/pgvector: **182 passed**.
- Dependency audits for inference, agent, and RAG/MCP requirements: no known vulnerabilities.
- Docker Compose configuration validation: passed.
- Inference API, agent runtime, and knowledge MCP images: built successfully.
- MCP and inference API container liveness smoke tests: passed.

This is evidence for the exact code commit above. It is not evidence of a deployed staging environment, a loaded model, production readiness, or measured service-level objectives. Any subsequent executable-code change requires a new exact-head CI run.

## Integration state

- PR #3 is the serving-hardening base on `main`; PR #7 is the hierarchical-agent branch stacked on PR #3; PR #17 is the RAG/MCP branch stacked on PR #7. PR #17 remains Draft and has not been merged.
- PR #8 has a stale base commit and is currently reported as not mergeable. PR #15 and PR #19 are stacked on that branch chain; reconcile/rebase that chain before treating its additional lifecycle controls as integrated.
- PR #20 is a separate alternative agent-runtime architecture with its own successful CI run. It is not an ancestor of PR #7 or PR #17; select and review one canonical runtime before merging overlapping implementations.
- GitHub branch-protection and required-review settings could not be verified with the current integration permissions. Confirm them in repository settings before any merge.

## Release gates

| Gate | Evidence required | Status |
|---|---|---|
| Inference API foundation | Green exact-head CI, schema/auth tests, container build and liveness smoke | CI passed on code commit 932a840; model readiness not verified |
| Real model artifact | Model ID/revision, license, checksum, tokenizer, chat template, reproducible loading | Blocked: model weights not supplied |
| Model quality and safety | Versioned benchmark dataset, task success, safety and regression evaluation | Blocked |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency, target-hardware soak test | Blocked |
| High-throughput serving | Runtime selection and measured load test for the selected model | Open |
| Agent runtime | End-to-end staging tasks, GitHub permissions, queue recovery, lease fencing, CI evidence freshness | Core runtime and lease fencing implemented; staging verification and lifecycle-control integration pending |
| Memory/RAG/MCP | Tenant-separated gold set, Recall@K/MRR/nDCG, citation accuracy, prompt-injection and adversarial cross-tenant tests | Code and pgvector/RLS checks implemented; latest Compose migration/role-switch smoke pending; domain evaluation and staging verification pending. Current custom-GUC RLS is defense in depth, not a boundary against a compromised shared worker credential; hard tenant isolation remains blocked on per-tenant DB identities or trusted signed DB assertions. |
| Security operations | TLS ingress, distributed rate limiting, egress allowlists, secret rotation, external penetration test | Partial; deployment controls required |
| Reliability operations | PostgreSQL backup/PITR, restore drill, immutable remote audit, monitoring, alerting, incident runbook, rollback rehearsal | Partial; operational drills pending |
| Production rollout | Staging soak, canary, SLO approval, owner sign-off | Not performed |

## Mandatory deployment checklist

- [ ] Supply and verify a licensed, versioned model artifact and tokenizer.
- [ ] Run model quality, safety, and target-hardware performance benchmarks.
- [ ] Use a TLS gateway with distributed rate limits, request timeouts, and network/egress controls.
- [ ] Store secrets in a secret manager; test tenant credential rotation and revocation.
- [ ] Back up PostgreSQL before applying the opt-in migration with `docker compose --profile migrate run --rm rusa-knowledge-migrate`; verify separate owner, worker, API, and restricted knowledge roles and forced RLS using the actual runtime identities.
- [ ] Configure encryption, backups, point-in-time recovery, and complete a restore drill.
- [ ] Ship HMAC audit records to immutable remote retention and test key rotation.
- [ ] Implement and adversarially test a tenant identity boundary that cannot be changed by arbitrary SQL under a shared worker credential (per-tenant DB roles/identities or trusted signed tenant assertions).
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
