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
- Tenant-scoped PostgreSQL coding-task queue with lease fencing, durable out-of-band
  cancellation, bounded cancellation recovery, advisory-locked terminal transitions,
  bounded manual retries, and production RLS/privilege-definition readiness checks.
- Stable tenant/task publication branches with lease-specific worktrees, idempotent
  draft-PR reconciliation, and explicit cleanup flags for cancelled external side effects.
- Cooperative worker shutdown and process-group termination when cancellation or
  lease revocation interrupts Git operations.
- Optional tenant-scoped idempotency keys for task creation. Replays with identical
  request payloads return the original task; payload changes under the same key
  return HTTP 409.
- Offline retrieval evaluation metrics: Recall@K, MRR@K, and NDCG@K.
- Optional RAG ingestion and hybrid retrieval using PostgreSQL/pgvector, with a
  separate read-only bearer-authenticated MCP server.
- Separate Python 3.11 CI job for RAG/MCP dependencies and pgvector integration tests.
- Isolated, non-root MCP container image with vulnerability scanning in CI.

## Verification boundary

Latest PR head: `843433149a14eb449f20d57e368d94c2838417ec` (PR #59).
CI run `42343798084` has passed Python compile, Ruff lint, Bandit, the main test
suite, and RAG/MCP integration tests. The API image and isolated sandbox image
have built, and the full test suite inside the sandbox has passed. The knowledge
MCP image, autonomous worker image, Trivy vulnerability gates, and production
Compose validation are still running; this ledger must be updated again after
those steps complete.

A local model backend is not a trained RUSA-AI model. No model weights are bundled.
The optional runtime can load a compatible, externally supplied, locally mounted
model artifact after manifest verification. Real-model throughput and quality
remain unmeasured until a specific checkpoint and target hardware are supplied.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| Integration CI | All jobs green on exact PR head, including Docker/Trivy and RAG/MCP | In progress: code tests and RAG/MCP pass; image scan/Compose pending |
| Local model artifact | Approved model ID/revision, license, tokenizer, pinned manifest digest | Blocked: no artifact supplied |
| Model quality | Versioned task dataset, safety evaluation, human review | Not measured |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency and soak results | Not measured |
| vLLM production | Compatible CUDA/PyTorch/vLLM versions and target-GPU smoke/load test | Not verified |
| Queue operations | PostgreSQL race, crash recovery, idempotency and cancellation tests | Automated tests pass on current PR head; production migration not applied |
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
- Apply AICode migrations `001` through `006` using the database owner.
  If RAG shares this database, apply `007_rag_knowledge_pgvector.sql` next on a
  pgvector-enabled database. Then apply `008_aicode_job_controls.sql` before
  enabling production task intake; it installs durable cancellation controls and
  bounded manual-retry counters. The API runtime must not have schema-owner or
  superuser privileges.
- If RAG uses a separate database, apply `007_rag_knowledge_pgvector.sql` there
  and configure the restricted `rusa_knowledge_worker` role independently.
- Keep MCP private behind TLS/network policy. Never store raw bearer tokens in
  `RUSA_MCP_TENANT_KEYS`; store SHA-256 digests mapped to tenant IDs.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- Do not describe the complete product as production-ready until every applicable
  release gate has objective evidence and human approval.
