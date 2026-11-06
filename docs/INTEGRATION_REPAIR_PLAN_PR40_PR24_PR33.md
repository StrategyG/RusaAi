# Integration repair plan: PR #40, #24, and #33

## Baseline and safety

- Integration branch: `integration/ci-unification-pr40-pr24-pr33`
- Baseline: `main` at `67e7cf7c0dbe0459ad126a8da2257ec6d867a9e3`
- Do not force-push or modify `main` as part of conflict resolution.
- Do not merge any of the three source PRs until the combined tree passes CI and review.

## Required merge order

1. PR #40: tenant-scoped idempotency and PostgreSQL worker/lease race regressions.
2. PR #24: local model artifact manifest verification, Transformers/vLLM runtime, and SSE streaming.
3. PR #33: deterministic runtime regression evaluation, RAG/MCP, and web workspace.

The PRs are not cleanly stackable: all three change shared files including
`.github/workflows/ci.yml`, `Dockerfile`, `docker-compose.yml`,
`llm-project/serve/app.py`, `pyproject.toml`, `requirements.txt`,
`.env.example`, `.dockerignore`, and `README.md`. PR #33 additionally
changes many of the same agent modules and tests as PR #40.

## CI reconciliation requirements

Keep the current main-branch controls; do not replace them with a PR workflow
that drops security gates. The unified workflow must retain:

- Ruff, compile, and pytest checks.
- Bandit static analysis.
- Production image Trivy reporting and blocking of fixable CRITICAL/HIGH issues.
- Isolated agent sandbox test execution.
- Production Docker Compose validation.
- PostgreSQL 16 service; use pgvector image/extension only for tests that
  actually require vector operations.
- Dependency audits for each shipped dependency set (base, agent, RAG/MCP,
  and optional vLLM) where those manifests exist in the integrated tree.
- Deterministic runtime benchmark and artifact upload.
- Build and smoke-test inference API, worker, RAG/MCP, and web workspace images.

## Shared-file conflict policy

- `ci.yml`: preserve all main security gates and add feature-specific checks.
- `app.py` and agent modules: reconcile behavior, not whole-file replacement;
  retain API compatibility and tenant/authentication boundaries.
- `Dockerfile` and Compose: preserve non-root execution, isolated sandbox,
  production secret handling, health checks, and existing production topology.
- Dependency manifests: combine compatible dependencies and pin/constraint
  conflicting major versions explicitly; audit the final resolved sets.
- Migrations: preserve ordering and verify clean-install plus upgrade paths.
- README and operations docs: describe only behavior verified by tests.

## Acceptance gates before merge to main

1. All three feature sets are present in the same integration commit.
2. Full unit/integration suite passes against PostgreSQL 16 and pgvector where
   required.
3. All image builds and runtime smoke tests pass.
4. Security scans pass; no existing main security gate is removed or weakened.
5. CI is green on the exact integration head SHA.
6. Human review approves the combined diff.
7. Only then open/retarget a reviewed PR to main and enable required status
   checks plus at least one approving review.

## Current limitation

The GitHub connector can create branches and commit file changes, but the
current source PRs contain overlapping, substantial application changes.
A workflow-only edit cannot safely resolve those code conflicts. The full PR
patches must be applied and reconciled against the integration branch, followed
by actual CI execution. This plan intentionally does not claim that the source
PR code has already been integrated or tested together.
