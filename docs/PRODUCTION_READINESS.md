# RUSA-AI Production Readiness Evidence Ledger

## Scope

This ledger describes the capabilities present on the integration branch and separates
automated code validation from deployment certification. Passing CI is evidence only
for the exact commit and checks that ran; it is not a production approval.

## Canonical shared knowledge-store decision (2026-11-12)

ADR-0002 selects RusaAi `rusa_knowledge` PostgreSQL/pgvector as the canonical
shared Agent/Worker knowledge store, subject to implementation and release gates.

- SQLite/FTS5 remains device-local memory/state; it is not a shared-corpus replica.
- RAG_Cerdas IM19 remains separate; dual-write, mirroring, bulk synchronization and
  cross-read are not authorized.
- RusaModels remains the Model Plane and is accessed through an authenticated API.
- The 1,536-dimensional and 384-dimensional vector spaces remain separate. The
  final production embedding profile is not selected.
- This decision does not authorize production migrations, ingestion, embeddings,
  index writes, publication or deployment.

Open RusaAi integration work requires reconciliation before merge:
- PR #82 proposes RAG agent/project scope and version history but is draft and
  introduces migration numbers that collide with current main and PR #109.
- PR #109 introduces inference admission migration 010.
- PR #111 is a combined CPU/inference integration candidate and must pass exact-head CI.
- All migrations must be re-sequenced into one reviewed, unique ordered inventory.

## Delivered in this integration branch

- OpenAI-compatible inference API with an optional local Transformers/vLLM backend.
- Local model artifact manifest and SHA-256 verification; production local loading
  requires an externally pinned manifest digest.
- Bounded generation/streaming adapters, API admission control, and backend shutdown.
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

The primary CI job has previously passed compile, lint, Bandit, and unit tests on
commit `7ed80f0801651165cd634b95dcd993a6c85b9484`. Subsequent commits added
RAG/MCP packaging, queue idempotency, race tests, and further CI changes; those
changes must be judged only by the latest workflow run on the exact PR head.

A local model backend is not a trained RUSA-AI model. No model weights are bundled.
The optional runtime can load a compatible, externally supplied, locally mounted
model artifact after manifest verification. Real-model throughput and quality
remain unmeasured until a specific checkpoint and target hardware are supplied.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| Integration CI | All jobs green on exact PR head, including Docker/Trivy and RAG/MCP | Pending latest run |
| Local model artifact | Approved model ID/revision, license, tokenizer, pinned manifest digest | Blocked: no artifact supplied |
| Model quality | Versioned task dataset, safety evaluation, human review | Not measured |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency and soak results | Not measured |
| vLLM production | Compatible CUDA/PyTorch/vLLM versions and target-GPU smoke/load test | Not verified |
| Queue operations | PostgreSQL race, crash recovery, idempotency and cancellation tests | Automated tests added; latest run pending |
| RAG/MCP deployment | Apply pgvector migration, restricted DB role, TLS/network controls, tenant-key provisioning | Code and tests added; deployment not performed |
| Knowledge governance | Rights, source versions, review, human approval, publication, deletion and audit lifecycle | Not implemented in full |
| Agent/project retrieval scope | Trusted runtime identity and cross-scope negative tests for search/get/list/history | Pending PR #82 reconciliation |
| Retrieval quality | Human-reviewed Indonesian/English gold set, citations, groundedness and security evaluation | Not measured |
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
- Apply `migrations/007_rag_knowledge_pgvector.sql` only on a pgvector-enabled
  knowledge database and configure the restricted `rusa_knowledge_worker` role.
  Apply `migrations/009_knowledge_embedding_384.sql` only when the separate
  384-dimensional profile is deliberately enabled and its model contract passes.
- Do not apply migrations introduced by open PRs until the combined migration
  inventory has unique numbering and its forward/rollback tests pass.
- Keep MCP private behind TLS/network policy. Never store raw bearer tokens in
  `RUSA_MCP_TENANT_KEYS`; store SHA-256 digests mapped to tenant IDs.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- Do not describe the complete product as production-ready until every applicable
  release gate has objective evidence and human approval.
