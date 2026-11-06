# Integration baseline audit — 2026-11-07

## Critical baseline finding

The source PRs #40, #24, and #33 are based on an older common ancestor and
have diverged substantially from current `main` (GitHub compare reported
512 commits behind for each source head at the audit point). Merging any source
branch wholesale risks reverting newer mainline behavior. This integration
therefore ports selected features into current APIs and migrations instead of
merging source histories.

## Shared-file incompatibilities

- `llm-project/serve/app.py`: the source branches have different application
  composition. The integration retains current API authentication, RBAC,
  request validation, admission control, and SSE framing, and adapts the local
  runtime through the existing provider protocol.
- `rusa_agents/queue.py`: source PR #40/#33 use a different task table and
  contract. The integration retains current `aicode_jobs` and ports
  tenant-scoped idempotency plus crash/lease/cancellation regression tests.
- `Dockerfile` and CI: current main uses Python 3.12, non-root runtime images,
  isolated sandbox tests, Bandit, and Trivy. These gates are preserved;
  optional RAG/MCP is tested in a separate Python 3.11 job and container.
- Migrations: current main already uses migration 005 for task-policy audit.
  The queue idempotency change is migration 006 and RAG/pgvector is migration
  007 to avoid duplicate sequence numbers.

## Features ported to the current integration branch

- PR #24: model artifact manifest schema/generator, SHA-256 verification,
  Transformers/vLLM runtime adapters, local provider adapter, model benchmark
  utility, optional dependency manifests, and inference operations runbook.
- PR #40: tenant-scoped `Idempotency-Key` replay semantics on task intake,
  conflict detection for changed payloads, production schema/index validation,
  and queue crash/lease/cancel/retry race tests adapted to `aicode_jobs`.
- PR #33: tenant-scoped RAG/pgvector store, bounded local document parsers,
  embedding provider, ingestion service/CLI, retrieval metrics, read-only
  bearer-authenticated MCP server/client, optional MCP image, dedicated tests,
  and operations documentation.

## Deliberately deferred

The source PR #33 identity/JWT subsystem and browser workspace are not ported.
They depend on a different API/authentication composition and database role
layout than current main. They require a separately designed auth migration,
front-end/API contract reconciliation, and end-to-end security review; copying
them wholesale would bypass current API-key/RBAC controls.

The alternate `rusa_agent_tasks` queue and source worker implementation are
also not copied. Their guarantees are being validated against current
`aicode_jobs`, lease tokens, and worker interfaces.

## Current verification boundary

Core CI previously passed compile, Ruff, Bandit, and unit tests on an earlier
integration commit. Since then, additional changes added the RAG/MCP job,
idempotency migration/API, queue race tests, and local model configuration
aliases. Only the latest CI result for the exact current PR head can validate
those changes. No merge to `main` has been performed.
