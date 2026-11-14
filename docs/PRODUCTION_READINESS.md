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

Current mainline migration inventory (RusaAi main SHA `1adac8d340e5870570910168b6626fe285dd2cf2`):

- Migrations 001–012 are present on main, including 010 inference admission, 011 agent/project scope, and 012 append-only knowledge version history.
- PR #111 is merged and must not be described as an open integration candidate.
- PR #82 remains a draft based on an older main SHA and proposes migration filenames that overlap the current mainline sequence; do not merge it wholesale.
- PR #109 remains open on a stacked architecture branch. Its migration 010 proposal must be compared with the current mainline migration 010; it is not evidence of an additional migration to apply.
- PR #115 proposes migration 013 for knowledge governance and remains draft/open. It is not part of mainline until reviewed and merged.

The repository migration files are not proof of the migration history in any deployed database. Verify the exact target's applied migration history read-only before planning any DDL. Reconcile all branch diffs into one unique ordered migration inventory and run forward/rollback/restore tests in disposable PostgreSQL/pgvector before any approved staging change.

## Delivered in this integration branch

- OpenAI-compatible inference API with an optional local Transformers/vLLM backend.
- Local model artifact manifest and SHA-256 verification; production local loading
  requires an externally pinned manifest digest.
- Bounded generation/streaming adapters, API admission control, and backend shutdown.
- CPU Compose worker inference routed through the API gateway with per-tenant API credentials, request deadlines, JSON response-format forwarding, and no automatic gateway retries (PR #108).
- PostgreSQL-backed FIFO inference admission with global slot rows, bounded waiting, heartbeat leases, crash recovery, stale-owner fencing, and API-only database privileges (PR #109; exact-head CI and staging verification pending).
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
gate, and Compose validation passed. The stacked worker-gateway change in
PR #108 passed 306 tests with 6 skipped plus the RAG/MCP integration job;
its Docker/sandbox/Trivy/Compose job is still being verified. PR #109 adds
the PostgreSQL-backed scheduler and must pass its own exact-head CI before
being considered verified. A successful result on an earlier PR does not
certify a later stacked commit.

A local model backend is not a trained RUSA-AI model. No model weights are bundled.
The optional runtime can load a compatible, externally supplied, locally mounted
model artifact after manifest verification. Real-model throughput and quality
remain unmeasured until a specific checkpoint and target hardware are supplied.

## Foundation checkpoint decision

The v2.0 `RusaAi-Edge-1.54B` definition is an architecture reference target;
it is not evidence that a compatible checkpoint exists. The Training Engine's
product identity remains `rusaai-edge-derived` with status
`CHECKPOINT_CANDIDATE_PENDING_AUDIT`.

`BhinekaIntiLabs/bhineka-gpt` is recorded in
`documentation/adr/ADR-TRAIN-001-foundation-checkpoint.md` as
`PILOT_ONLY`, not as a production dependency. Its published model card reports
556,269,696 parameters and 2,048-token context, declares Apache-2.0 metadata,
and warns that the custom architecture may require `trust_remote_code=True`.
The model-card validation is CUDA language-modeling loss, not CPU or
instruction-following certification. The immutable revision, artifact hashes,
lineage/license/data audit, custom-code review, CPU GGUF benchmark, and safety
evaluation remain open.

The historical Qwen3-4B CPU v1.0 profile is not silently restored. A production
release still requires a compatible non-Qwen checkpoint and its complete
release manifest.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| Integration CI | All jobs green on the exact candidate SHA, including Docker/Trivy and RAG/MCP | Current main SHA `1adac8d` has 4/4 check runs passing. PR #107 has 3/3 passing on an older base SHA; PR #109 has 2 passing and 1 cancelled check on a stacked branch; PR #111 is merged with 4/4 observed checks passing. CI does not certify deployment. |
| Inference contract integration | Endpoint and worker validate v1 request/response schemas, stable IDs, model identity, error semantics, and measured performance fields | Schema files exist, but README confirms the OpenAI-compatible endpoint does not yet consume them; blocked |
| Local model artifact | Approved non-Qwen model ID/revision, license, tokenizer, pinned manifest digest | Blocked: no production checkpoint; Bhineka-GPT is PILOT_ONLY pending audit |
| Model quality | Versioned task dataset, safety evaluation, human review | Not measured |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency and soak results | Not measured |
| vLLM production | Compatible CUDA/PyTorch/vLLM versions and target-GPU smoke/load test | Not verified |
| Queue operations | PostgreSQL race, crash recovery, idempotency and cancellation tests | PR #107 unit/integration CI passed; staging recovery drill still required |
| Shared inference admission | API and worker use one gateway; multi-process fairness, cancellation, restart and crash recovery | Migration 010 and PostgreSQL scheduler are present on main; PR #109 remains open on a stacked branch and its security check was cancelled. Cross-process FIFO evidence and staging crash/restart drill remain required. |
| RAG/MCP deployment | Apply pgvector migration, restricted DB role, TLS/network controls, tenant-key provisioning | Code and tests added; deployment not performed |
| Knowledge governance | Rights, source versions, review, human approval, publication, deletion and audit lifecycle | Initial governance is proposed in PR #115; purge, index-generation reconciliation and erasure remain open |
| Agent/project retrieval scope | Trusted runtime identity and cross-scope negative tests for search/get/list/history | Migrations 011/012 are present on main and PR #111 is merged; deployed-database PostgreSQL ACL and cross-scope negative evidence are still required. |
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
- Preserve the current mainline migration inventory 001–012. Migration 013 is proposed in open PR #115 and is not on main. Do not apply PR-only migrations to staging or production. Reconcile every branch and the actual applied database history, then run disposable PostgreSQL/pgvector forward/rollback/restore tests before any approved staging migration. The API runtime must not have schema-owner or superuser privileges.
- Apply `migrations/007_rag_knowledge_pgvector.sql` only on the designated
  pgvector-enabled knowledge database and configure the restricted
  `rusa_knowledge_worker` role. Apply `migrations/009_knowledge_embedding_384.sql`
  only when the separate 384-dimensional profile is deliberately enabled and
  its model contract passes.
- Keep MCP private behind TLS/network policy. Never store raw bearer tokens in
  `RUSA_MCP_TENANT_KEYS`; store SHA-256 digests mapped to tenant IDs.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- Production worker inference must route through the API gateway using tenant-
  scoped credentials; the worker must not call the model endpoint directly.
- Apply `migrations/010_inference_admission.sql` and ensure the scheduler config
  row matches the same active slots, waiting limit, queue timeout, and lease
  duration on every API replica. Provision one slot row per configured active
  slot. Startup fails closed on a mismatch.
- Production worker gateway credentials must be tenant-specific, at least 32 bytes,
  stored in the secret manager, and SHA-256-bound to matching inference-capable
  principals in RUSA_API_KEYS_JSON. The worker must fail closed for missing mappings.
- Production Compose requires `RUSA_WORKER_INFERENCE_BASE_URL` to point to a
  trusted HTTPS API gateway and mounts `RUSA_INTERNAL_CA_BUNDLE` into the worker.
  The operator must provide and verify the TLS endpoint/certificate chain; the
  Compose overlay does not create that proxy itself.
- Do not describe the complete product as production-ready until every applicable
  release gate has objective evidence and human approval.
