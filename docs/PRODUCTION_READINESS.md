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
- Tenant-scoped PostgreSQL coding-task queue with lease tokens, expiry recovery,
  fencing of stale acknowledgements, cooperative cancellation with a monotonic
  cancel epoch, bounded heartbeat/lease renewal, ambiguous-side-effect
  reconciliation, and retry limits.
- Optional tenant-scoped idempotency keys for task creation. Replays with identical
  request payloads return the original task; payload changes under the same key
  return HTTP 409.
- Offline retrieval evaluation metrics: Recall@K, MRR@K, and NDCG@K.
- Optional RAG ingestion and hybrid retrieval using PostgreSQL/pgvector, with a
  separate read-only bearer-authenticated MCP server.
- Separate Python 3.11 CI job for RAG/MCP dependencies and pgvector integration tests.
- Versioned, transport-neutral platform task/result/run-event schemas for the four
  domains, ten core-agent roles, and eight DevOps lifecycle stages, with Python
  validation for evidence completeness, event ordering, and contract safety.
- Isolated, non-root MCP container image with vulnerability scanning in CI.

## Verification boundary

The complete RUSA-AI CI and Web CI workflows passed on code commit
`df915f2f7cb8b05040f1f6eab98970ac5781f427` (RUSA-AI run
`42588833644`; Web run `42588833642`). The main test job reported
**300 passed, 6 skipped**; compile, lint, Bandit, SFT dataset validation,
RAG/MCP integration, API/sandbox/worker/MCP image builds, Trivy vulnerability
gates, Compose validation, web type-check, and Next.js/container build all
passed. The six skips were optional-dependency or optional-database tests,
not failures.

This evidence applies to that exact code commit. Every later commit must be
judged by the latest workflow runs on the exact PR head; a previous green run
does not certify a newer commit.

A local model backend is not a trained RUSA-AI model. No model weights are bundled.
The optional runtime can load a compatible, externally supplied, locally mounted
model artifact after manifest verification. Real-model throughput and quality
remain unmeasured until a specific checkpoint and target hardware are supplied.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| Integration CI | All jobs green, including Docker/Trivy and RAG/MCP | Passed on code commit `df915f2f7cb8b05040f1f6eab98970ac5781f427`; rerun required for later commits |
| Local model artifact | Approved model ID/revision, license, tokenizer, pinned manifest digest | Blocked: no artifact supplied |
| Model quality | Versioned task dataset, safety evaluation, human review | Not measured |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency and soak results | Not measured |
| vLLM production | Compatible CUDA/PyTorch/vLLM versions and target-GPU smoke/load test | Not verified |
| Queue operations | PostgreSQL race, crash recovery, idempotency, cancellation acknowledgement and ambiguous-side-effect reconciliation | Passed in the 300-test main suite on code commit `df915f2f7cb8b05040f1f6eab98970ac5781f427` |
| RAG/MCP deployment | Apply pgvector migration, restricted DB role, TLS/network controls, tenant-key provisioning | Code and tests added; deployment not performed |
| Identity and control center | Browser control-center UI exists; integrated enterprise JWT/SSO, user administration and full end-to-end identity tests | Partially implemented; enterprise identity not verified |
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
- Apply AICode migrations `001` through `006`, then
  `009_aicode_blocked_status.sql`, `010_aicode_cooperative_cancellation.sql`,
  `011_aicode_cancellation_reconciliation.sql`, and
  `012_aicode_task_list_audit.sql` in order using the database owner before
  enabling production task intake. The API runtime must not have schema-owner
  or superuser privileges. Migration `012` is the final globally unique
  task-audit migration; it preserves both `task.list` and `task.reconcile`.
- Apply `migrations/007_rag_knowledge_pgvector.sql` followed by
  `migrations/008_rag_embedding_profiles.sql` on the pgvector-enabled knowledge
  database and configure the restricted `rusa_knowledge_worker` role. The
  RAG migration `008` is independent of the AICode migration sequence.
- Keep MCP private behind TLS/network policy. Never store raw bearer tokens in
  `RUSA_MCP_TENANT_KEYS`; store SHA-256 digests mapped to tenant IDs.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- Do not describe the complete product as production-ready until every applicable
  release gate has objective evidence and human approval.
