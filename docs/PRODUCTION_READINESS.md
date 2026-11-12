# RUSA-AI Production Readiness Evidence Ledger

## Scope

This ledger describes the capabilities present on the integration branch and separates
automated code validation from deployment certification. Passing CI is evidence only
for the exact commit and checks that ran; it is not a production approval.

## Delivered in this integration branch

- OpenAI-compatible inference API with an optional local Transformers/vLLM backend.
- Local model artifact manifest and SHA-256 verification; production local loading
  requires an externally pinned manifest digest.
- Bounded generation/streaming adapters, API admission control, and backend shutdown.
- CPU Compose worker inference routed through the API gateway with per-tenant API credentials, request deadlines, JSON response-format forwarding, and no automatic gateway retries (PR #108; CI/staging verification pending).
- Tenant-scoped PostgreSQL coding-task queue with lease tokens, expiry recovery,
  fencing of stale acknowledgements, cancellation/completion race protection,
  and retry limits.
- Optional tenant-scoped idempotency keys for task creation. Replays with identical
  request payloads return the original task; payload changes under the same key
  return HTTP 409.
- Offline retrieval evaluation metrics: Recall@K, MRR@K, and NDCG@K.
- Optional RAG ingestion and hybrid retrieval using PostgreSQL/pgvector, with a
  separate read-only bearer-authenticated MCP server.
- Separate Python 3.11 CI job for RAG/MCP dependencies and pgvector integration tests.
- Isolated, non-root MCP container image with vulnerability scanning in CI.

## Verification boundary

Architecture PR #107 passed all jobs on exact head
`117da0ef39a38a096e28cc3d9712c95a8fef4f8a` in workflow run
[43100849792](https://github.com/StrategyG/RusaAi/actions/runs/43100849792):
293 passed, 6 skipped; compile, Ruff, Bandit, curated SFT validation,
RAG/MCP integration, isolated sandbox tests, image builds, Trivy release
gate, and Compose validation passed. This result certifies only that PR #107
commit and its executed checks. The stacked worker-gateway change in PR #108
must pass its own full workflow on its exact head before it can be considered
verified.

A local model backend is not a trained RUSA-AI model. No model weights are bundled.
The optional runtime can load a compatible, externally supplied, locally mounted
model artifact after manifest verification. Real-model throughput and quality
remain unmeasured until a specific checkpoint and target hardware are supplied.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| Integration CI | All jobs green on exact PR head, including Docker/Trivy and RAG/MCP | PR #107 passed; PR #108 exact-head CI pending |
| Local model artifact | Approved model ID/revision, license, tokenizer, pinned manifest digest | Blocked: no artifact supplied |
| Model quality | Versioned task dataset, safety evaluation, human review | Not measured |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency and soak results | Not measured |
| vLLM production | Compatible CUDA/PyTorch/vLLM versions and target-GPU smoke/load test | Not verified |
| Queue operations | PostgreSQL race, crash recovery, idempotency and cancellation tests | PR #107 unit/integration CI passed; staging recovery drill still required |
| Shared inference admission | API and worker use one tenant-authenticated gateway; multi-process fairness, cancellation, restart and crash recovery | CPU and production Compose worker routing are configured in PR #108; production TLS trust and tenant secrets require deployment verification; API queue remains process-local pending distributed scheduler (PR #109) |
| RAG/MCP deployment | Apply pgvector migration, restricted DB role, TLS/network controls, tenant-key provisioning | Code and tests added; deployment not performed |
| Identity/web workspace | Integrated JWT identity, browser UI, user administration and end-to-end tests | Not included in this integration branch |
| Security operations | Independent tenant-isolation review, penetration testing, secret rotation and TLS verification | Deployment review required |
| Reliability operations | Backup/restore drill, rollback rehearsal, alerting and incident runbook | Not completed |
| Production rollout | Staging soak, canary, SLO approval and owner sign-off | Not performed |

## Deployment rules

- Do not merge while any required CI job is pending or failing.
- Do not deploy the local backend without a verified model artifact and a pinned
  `RUSA_MODEL_MANIFEST_SHA256` value in the secret/configuration manager.
- Build the API image with `--build-arg RUSA_INSTALL_LOCAL_MODEL=1` when local
  Transformers dependencies are required. The default image intentionally omits
  heavyweight model packages.
- Apply migrations `001` through `006` in order using the database owner before
  enabling production task intake. The API runtime must not have schema-owner or
  superuser privileges.
- Apply `migrations/005_rag_knowledge_pgvector.sql` only on a pgvector-enabled
  knowledge database and configure the restricted `rusa_knowledge_worker` role.
- Keep MCP private behind TLS/network policy. Never store raw bearer tokens in
  `RUSA_MCP_TENANT_KEYS`; store SHA-256 digests mapped to tenant IDs.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- The CPU Compose profile routes worker inference through the API gateway. This
  shares admission only within the single API process; keep one API replica per
  model endpoint until a durable cross-process scheduler is implemented and tested.
- Production worker gateway credentials must be tenant-specific, at least 32 bytes,
  stored in the secret manager, and SHA-256-bound to matching inference-capable
  principals in RUSA_API_KEYS_JSON. The worker must fail closed for missing mappings.
- Do not describe the complete product as production-ready until every applicable
  release gate has objective evidence and human approval.
