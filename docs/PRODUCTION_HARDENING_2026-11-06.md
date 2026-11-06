# RUSA-AI production hardening — 2026-11-06

## Scope

This change set is stacked on the hierarchical agent runtime branch. It preserves tenant-specific repository allowlists and lease-token fencing while adding evidence, recovery, and input-boundary controls.

## Implemented

- Durable checkpoints: each worker result and orchestration stage is persisted to PostgreSQL only while the exact lease token is current and unexpired.
- Lease-safe recovery: stale workers cannot overwrite checkpoints after a lease is reclaimed.
- Security evidence: Security must inspect a bounded, complete GitHub compare diff and CI evidence tied to the same commit.
- TOCTOU closure: after the model-based security review, the runtime re-reads the diff and CI and requires unchanged base/head SHAs.
- CI evidence integrity: check-runs are paginated with hard page limits, duplicate reruns are reduced to the latest run per app/name, and incomplete pagination fails closed.
- Secret minimization: repository file reads and diff patches redact private keys, common access tokens, JWTs, and credential assignments. A diff containing redacted credential material cannot be auto-approved.
- Tenant branch binding: the GitHub gateway rejects a task whose work branch is not bound to its tenant and idempotency/task identity.
- Database readiness: API and worker readiness require forced RLS and the expected tenant policy for both queue and memory.
- Request intake: the API enforces both a byte limit and a total body-intake timeout.
- CI coverage: pull requests targeting stacked branches run the same checks; Compose interpolation is validated in CI.

## Verification gates

The change is not certified production-ready until the new branch CI passes, including PostgreSQL integration tests, lint, compile, dependency audit, and container build/smoke checks. A live deployment additionally requires:

1. Branch protection on the default branch with required successful checks and review.
2. A least-privilege GitHub App token scoped to approved repositories and required read/write/PR/check permissions only.
3. Real provider credentials delivered through a secret manager, not a committed environment file.
4. External immutable audit retention and tested backup/restore.
5. Multi-worker load/soak testing, provider/GitHub rate-limit testing, and failure/recovery drills.
6. A versioned model-quality benchmark with human-reviewed expected outcomes.
7. A staged deployment, rollback procedure, monitoring, and alert thresholds.

No merge, production deployment, or credential/permission change is performed by this change set.
