# ADR-0002: Canonical Shared Agent/Worker Knowledge Store

- Date: 2026-11-12
- Status: Accepted by the project owner for implementation planning; production activation is not authorized
- Decision owner: RUSA AI Project Owner
- Scope: Shared Agent/Worker knowledge storage and retrieval in RusaAi
- Related: ADR-0001 (RusaAi-Edge v2.0 CPU-first baseline, currently proposed in PR #107); RAG retrieval contract; RusaModels Model Plane API contract

## Context

The RUSA AI architecture has three distinct storage/runtime concerns that must not be conflated:

1. Device-local memory and state using SQLite/FTS5.
2. The optional shared RusaAi knowledge service using PostgreSQL, pgvector and PostgreSQL full-text search.
3. RAG_Cerdas IM19, a separate candidate knowledge system with source-version, claim/evidence, review and approval concepts.

The inspected RusaAi main branch already contains:

- `rusa_knowledge_documents` and `rusa_knowledge_chunks` in migration `007_rag_knowledge_pgvector.sql`.
- Tenant-scoped primary/foreign keys and forced row-level security (RLS).
- PostgreSQL `TSVECTOR` full-text search with a GIN index.
- A 1,536-dimensional pgvector column and HNSW cosine index.
- A separate optional 384-dimensional embedding column and HNSW index in migration `009_knowledge_embedding_384.sql`.
- Tenant-authenticated, read-only Knowledge MCP tools.
- An optional RAG adapter for the AICode Worker Tool Gateway, disabled by default.

The current RusaAi store is therefore the most direct implementation base for the shared Agent/Worker corpus. It is not yet a fully governed, production-activated knowledge platform. Its current mainline schema does not provide the complete source-rights, source-version, review, approval, publication, deletion and agent/project authorization lifecycle.

RAG_Cerdas IM19 is a separate candidate. Its canonical-store ADR remains proposed and its tenant/project isolation and service boundary are not yet approved for multi-tenant production use.

## Decision

**Select Option A: RusaAi PostgreSQL + pgvector (`rusa_knowledge`) as the canonical shared Agent/Worker knowledge store.**

The following boundaries are mandatory:

1. RusaAi `rusa_knowledge` is the single canonical writer and answer-facing shared retrieval store after the applicable implementation and release gates pass.
2. SQLite/FTS5 remains device-local memory/state and local full-text retrieval. It is not a second writer or a replica of the shared corpus.
3. RAG_Cerdas IM19 is not selected as the production writer or retrieval backend under this ADR. No dual-write, mirroring, bulk synchronization or cross-read is permitted.
4. RusaModels remains the Model Plane for inference and embedding requests through an authenticated versioned API. RusaAi must not access RusaModels databases, model storage credentials or internal filesystem paths directly.
5. The RAG store is a knowledge/evidence retrieval system. It is not a training-data approval shortcut and does not automatically trigger model training or fine-tuning.
6. The current optional RAG service remains disabled unless explicitly enabled in a controlled environment after its database, credentials, network, rights and evaluation gates pass.

## Embedding decision boundary

This ADR selects the store, not the final embedding model.

- The current RusaAi default is the 1,536-dimensional `embedding` path.
- The 384-dimensional `embedding_384` path is a separate optional vector space.
- Vectors from the two spaces must never be mixed in the same query/index generation.
- The model profile, immutable model revision, artifact checksum, tokenizer/preprocessing, pooling, normalization, query/passage behavior and dimension must be recorded for every index generation.
- A production embedding profile requires a controlled Indonesian/English retrieval benchmark, resource measurements and explicit approval.
- The existing RusaModels local CPU embedding service may be used for the 384-dimensional pilot only after its artifact revision and checksum are pinned and validated.

## Required implementation work

### 1. Migration and branch reconciliation

The current main branch has migrations 001–009, including 008 and 009. Open integration work introduces additional migration files:

- PR #82 proposes migrations 008, 009 and 010 for RAG agent/project scope and version history.
- PR #109 introduces migration 010 for inference admission.
- PR #111 is a combined CPU/inference integration candidate and includes migration 010.

Before any of these changes are integrated:

1. Rebase the RAG work onto the selected integration base.
2. Produce one reviewed migration inventory with unique, ordered filenames and explicit dependencies.
3. Resolve duplicate migration numbers and verify the actual applied migration history in the target database.
4. Preserve additive/rollback-safe behavior; do not drop legacy vector columns or overwrite source provenance.
5. Run migration tests in a disposable PostgreSQL/pgvector database.
6. Do not apply migrations to staging or production as part of this ADR.

No new production migration is authorized by this decision. The next schema migration must be assigned only after the open PR dependency graph and migration inventory are reconciled.

### 2. Knowledge governance lifecycle

Extend the selected RusaAi store with explicit, auditable states and relationships for:

- Source identity, owner, rights/license, permitted uses and retention.
- Immutable source versions, commits/revisions and source SHA-256.
- Document and chunk versions, stable IDs and exact page/line/section/symbol locators.
- Agent scope and Worker scope as independent dimensions.
- Project scope derived from trusted repository/task authorization.
- Technical, source-fidelity, rights, security and QA review findings.
- Human approval and explicit publication authorization.
- Embedding model/revision and index-generation lineage.
- Ingestion jobs, checkpoints, retries and reconciliation.
- Source withdrawal, tombstones, deletion propagation and audit events.

A candidate record, embedding, vector point or database row is not publication authorization. Only records with the required rights, review, human approval, publication authorization, ACL and evaluation state may appear in answer-facing production retrieval.

### 3. Trusted scope and tenant isolation

- Keep current tenant RLS and the restricted `rusa_knowledge_worker` role.
- Add project and agent/worker scope only from authenticated runtime identity and the registered role/project grants.
- Never trust model-generated `tenant_id`, `project_id`, `agent_id` or `worker_role` as an authorization source.
- Enforce the same authorization policy for search, get, list, collection counts, version history and deletion.
- Apply ACL predicates before candidate retrieval and validate returned evidence again before it reaches the model.
- Treat retrieved content as untrusted evidence, never as system instructions or tool authority.

PR #82 is a candidate implementation, not approved production code. It must be rebased and reviewed against this ADR, and its migration numbering must be reconciled before merge.

### 4. Retrieval and evaluation

- Preserve the existing PostgreSQL hybrid retrieval baseline (full-text plus pgvector and weighted Reciprocal Rank Fusion) until a benchmark supports a change.
- Start with a small evidence set (3–6 chunks) and expand only when necessary.
- Preserve source version, exact locator, content hash, publication state and citation in every returned evidence item.
- Build a human-reviewed Indonesian/English gold set and measure Recall@K, MRR, NDCG@K, citation correctness, groundedness, stale-source behavior, ACL isolation, prompt-injection resistance and retrieval latency.
- Do not invent a retrieval acceptance threshold before measuring a reproducible baseline.

## Alternatives considered

### Option B — RAG_Cerdas IM19

Not selected as the canonical production writer at this time. Its source/version/claim/evidence/review/approval structures are valuable design references, but tenant/project RLS, least-privilege service roles and a production service contract must be completed before it can safely serve a multi-tenant runtime. It remains isolated; this ADR does not authorize data movement into or out of IM19.

### Option C — SQLite/FTS5 only

Not selected for the shared corpus. SQLite/FTS5 remains appropriate for device-local state and local retrieval, but this ADR does not treat it as the shared multi-agent corpus or the five-million-unit production store.

## Consequences

Positive:

- One shared Agent/Worker knowledge writer and retrieval authority.
- Reuse of the existing RusaAi PostgreSQL/pgvector, full-text, RLS and MCP implementation.
- Clear separation between local memory, shared knowledge and model runtime.
- No ambiguity about dual-write or cross-store retrieval.

Required work and risks:

- The RusaAi schema must gain the missing governance and version lifecycle.
- Agent/project scope must be enforced by trusted identity, not just caller filters.
- Open PR migration conflicts must be resolved before integration.
- Production activation remains blocked until rights, security, retrieval quality, operations and human approval gates pass.

## Release gates

Production answer-facing activation is **NO-GO** until all applicable gates have objective evidence:

- Canonical schema and migration inventory approved.
- Migration forward/rollback and restore tests pass.
- Source rights and retention are known and authorized.
- Tenant, project, agent and Worker isolation pass positive and negative tests.
- Human approval and publication authorization are enforced.
- Embedding model and index generation are pinned and reconciled.
- Retrieval quality, citation, groundedness and prompt-injection tests pass.
- Deletion/withdrawal propagates to chunks, lexical/vector indexes, caches and retention-controlled backups.
- Backup/restore, monitoring, incident response and rollback are verified.
- Independent QA, Security and DevOps reviews pass.
- Project Owner explicitly approves the production release.

## Explicit non-decisions

This ADR does not:

- Approve a specific embedding model or dimension for production.
- Select a new vector database; PostgreSQL/pgvector remains the selected store implementation.
- Authorize production migration, ingestion, embedding, vector writes or deployment.
- Approve Batch 033 candidate records for publication.
- Approve or merge PR #82, #107, #109 or #111.
- Claim that the RAG service is deployed or production-active.
- Grant autonomous agents permission to change database schemas, policies, secrets or production data.

**Final decision:** Option A is selected as the canonical shared knowledge-store architecture. Implementation is staged and review-gated; production RAG remains NO-GO.
