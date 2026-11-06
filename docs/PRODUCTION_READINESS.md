# RUSA-AI Production Readiness Evidence Ledger

## Current baseline

- Repository: `StrategyG/RusaAi`
- Current main commit at this ledger revision: `3c012e0a4342b8e10b974643884539618d222aee`
- PR #50 (post-lease Git publication fencing): merged into main.
- PR #50 exact-head CI: passed (test/lint/security, production image build/test/scan, and RAG/MCP integration).
- Main push CI for commit `3c012e0`: passed all three jobs (test/lint/security, production image build/test/scan, and RAG/MCP integration).

A green CI run validates only the exact commit and checks it executed. It does not certify a VPS deployment.

## Implemented capabilities

- FastAPI OpenAI-compatible inference gateway with bounded requests, authentication,
  RBAC, admission control, and streaming.
- Optional local Transformers/vLLM runtime with verified model manifests and
  externally pinned SHA-256 digest support.
- Tenant-scoped PostgreSQL AICode queue, lease tokens, lease expiry recovery,
  stale-lease fencing, cancellation/completion race protection, retry limits,
  and tenant-scoped idempotency-key replay protection.
- Sandboxed autonomous worker with a dedicated rootless Docker daemon contract.
- Optional PostgreSQL/pgvector knowledge ingestion and hybrid retrieval.
- Read-only bearer-authenticated MCP server with tenant identity derived from
  token hashes.
- CI compile, lint, Bandit, PostgreSQL-backed tests, Docker image builds, Trivy
  vulnerability reporting/blocking for fixable CRITICAL/HIGH findings, and
  separate pgvector/MCP integration tests.

## Explicit product boundaries

- No trained RUSA-AI model weights or tokenizer are bundled. The local inference
  runtime is a loader, not a trained model. Chat requires an approved external
  OpenAI-compatible endpoint until an approved local checkpoint is supplied.
- The MCP/RAG service is separate from the core API and must be deployed
  independently. The core API does not automatically expose MCP tools.
- A browser workspace, user administration UI, and integrated JWT identity
  subsystem are not included in the current mainline.
- The proposed eight-stage Agentic DevOps lifecycle is not yet implemented as
  eight deployable agents. Current autonomous execution is the AICode worker
  path; Plan, Build, Test, Release, Deploy, Operate, Monitor agents and their
  dedicated DevOps Orchestrator/Security Guard remain implementation work.
- The uploaded communication ADR is still a proposed target: the current
  runtime uses the PostgreSQL task queue, not RabbitMQ + Kafka, a transactional
  workflow outbox/event log, gRPC/Protobuf agent RPC, or signed durable messages.
  These must not be represented as delivered capabilities.
- The current CI builds and scans images but does not publish immutable images
  to GHCR automatically.

## Release gates

| Gate | Evidence required | Status |
|---|---|---|
| Exact-main CI | All jobs green on commit `3c012e0` | Passed (all three jobs) |
| VPS infrastructure | Provisioned Linux host, rootless Docker worker daemon, storage, DNS | Not supplied / not deployed |
| Agentic DevOps lifecycle | Eight specialist agents, durable orchestrator workflow, validated task/result/event contracts, security guard | Not implemented; roadmap required |
| Egress enforcement | Worker/sandbox outbound allowlist or egress proxy with deny-by-default policy | Not implemented in Compose; mandatory before untrusted execution |
| Database | PostgreSQL 16 + pgvector, encrypted backup/PITR, TLS, separate runtime logins | Not provisioned |
| Migrations | Apply 001–006, then 007 and 008 in order; verify RLS and role grants | Not applied |
| Images | Build/push API, worker, sandbox, MCP; record immutable SHA-256 digests | Manual registry publication required |
| Chat model | Approved endpoint or verified local model artifact and task-quality tests | Not configured; no local weights |
| Embeddings | Approved 1536-dimensional OpenAI-compatible embeddings endpoint | Not configured |
| MCP | Dedicated read-only login, tenant token hashes, TLS ingress, ready check | Deployment pending |
| AICode worker | Rootless sandbox execution, GitHub least-privilege token, repo allowlist | Deployment pending |
| Branch protection | Required PR review and CI checks on main | Must be verified by repository administrator |
| Operations | Monitoring, alerting, encrypted backup, restore drill, rollback rehearsal | Not completed |
| Security | Independent tenant-isolation and sandbox review, secret rotation, TLS checks | Not completed |
| Production approval | Staging soak, SLO approval, human operator sign-off | Not approved |

## Deployment rules

- Keep `RUSA_AICODE_ENABLED=false` until all deployment and operational gates
  pass. Do not expose the worker to untrusted tenants before sandbox, GitHub,
  database, and tenant-isolation checks pass.
- Keep `RUSA_RAG_ENABLED=false` until migrations 007 and 008, embedding access,
  and reader-role grants have been verified.
- Use `rusa_knowledge_reader` for MCP and autonomous retrieval. Reserve the
  write-capable `rusa_knowledge_worker` role for a separately controlled
  ingestion identity.
- Apply migrations 001–006 in numeric order, followed by 007 and 008. Never run
  migrations using an application login.
- Deploy only immutable image digests. The API, worker, sandbox, and MCP images
  must be built from the same reviewed source revision.
- Do not deploy the local model backend without a verified artifact manifest
  and externally pinned `RUSA_MODEL_MANIFEST_SHA256`.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- Do not describe RUSA-AI as a standalone trained model or production-certified
  platform until the applicable gates have objective evidence and operator
  approval.
