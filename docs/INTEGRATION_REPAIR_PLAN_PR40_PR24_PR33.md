# Integration repair plan: PR #40, #24, and #33

## Baseline and safety

- Integration branch: `integration/ci-unification-pr40-pr24-pr33`
- Base: current `main` (at branch creation: `67e7cf7c0dbe0459ad126a8da2257ec6d867a9e3`)
- Do not force-push, modify `main`, or merge source PR histories.
- Source PRs are substantially behind current main; port features by contract and test.

## Port status

| Source | Feature | Status |
|---|---|---|
| PR #40 | Tenant-scoped idempotency keys | Ported to current API/queue; migration 006 |
| PR #40 | Lease fencing, worker crash recovery, cancellation and retry race regressions | Adapted tests added for current `aicode_jobs` |
| PR #24 | Model manifest schema and SHA-256 verification | Ported |
| PR #24 | Transformers/vLLM runtime and streaming adapter | Ported; requires optional model dependencies and an externally supplied model |
| PR #24 | Model benchmark and artifact manifest utilities | Ported |
| PR #33 | RAG chunking, parsers, embeddings, pgvector store and ingestion service | Ported; migration 007 |
| PR #33 | Retrieval Recall@K, MRR and NDCG evaluation | Ported |
| PR #33 | Read-only tenant-authenticated MCP server/client | Ported as an isolated optional service |
| PR #33 | RAG/MCP tests, image and operations documentation | Ported; dedicated CI job added |
| PR #33 | Identity/JWT subsystem and web workspace | Deferred; incompatible auth/API model requires separate design |

## CI reconciliation

The current main workflow is preserved and extended:
- Python 3.10 compile, Ruff, pytest, and Bandit remain in the core job.
- Existing API, worker and sandbox image builds, Trivy reporting/blocking,
  isolated sandbox suite, and production Compose validation remain.
- Separate Python 3.11 + pgvector job tests optional RAG/MCP modules.
- The isolated RAG/MCP image is built and included in Trivy checks.

## Migrations

Apply the current core migrations in order:
1. `001_aicode_core.sql`
2. `002_aicode_lease_fencing.sql`
3. `003_aicode_task_results.sql`
4. `004_aicode_tenant_rls.sql`
5. `005_aicode_task_policy_audit.sql`
6. `006_aicode_idempotency.sql`

Apply `007_rag_knowledge_pgvector.sql` only to a PostgreSQL database with
pgvector enabled and the dedicated restricted knowledge role. Do not confuse
the RAG migration with the existing core migration 005.

## Acceptance gates before merge to main

1. All included feature ports are present on one integration head.
2. Core CI and the RAG/MCP CI job pass on the exact final PR head.
3. All Docker builds, Trivy checks, and production Compose validation pass.
4. PostgreSQL tests cover tenant isolation, idempotency replay/conflict,
   expired-lease recovery, stale-token fencing, cancellation/completion races,
   and retry limits.
5. Local model smoke tests pass with an approved artifact on target hardware.
6. Human review approves the combined diff and confirms branch protection
   requires the applicable checks and an approving review.
7. Only then convert PR #49 from draft and merge through normal repository
   review/branch-protection controls.

## Remaining release work

- Complete the identity/JWT and web workspace integration separately.
- Run real-model latency, throughput, cancellation, and soak tests.
- Apply migrations and test restore/rollback on a staging database.
- Perform independent tenant-isolation and deployment security review.

This plan records integration status; it is not a production certification.
