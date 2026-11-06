# RUSA-AI production hardening — 2026-11-06

## Implemented controls

- Lease-fenced PostgreSQL checkpoints for worker and orchestration progress.
- Forced tenant RLS and named tenant policies required by readiness checks for queue and memory.
- Tenant/task-bound work-branch identity and per-tenant repository allowlists.
- Paginated GitHub check-run evidence with unique IDs, latest-rerun selection, and fail-closed completeness checks.
- Bounded GitHub compare diffs with protected-path rejection, patch/response limits, and credential redaction.
- A separate read-only security-review pass over the collected diff and CI evidence.
- Final diff and CI revalidation after review, requiring unchanged base and head commit SHAs.
- Configurable request-body intake timeout and GitHub evidence limits.
- CI validation of Docker Compose interpolation and pull-request checks on stacked branches.

## Release verification

The branch is not certified production-ready until all branch CI checks pass. A live production rollout additionally requires:

1. Required branch protection and human review on the default branch.
2. A least-privilege GitHub App installation/token limited to approved repositories and the minimum required permissions.
3. Real provider and database credentials supplied through a secret manager.
4. External immutable audit retention, backups, and a tested restore procedure.
5. Multi-worker load/soak tests, provider/GitHub rate-limit tests, and worker crash/lease-recovery drills.
6. A versioned model-quality benchmark with human-reviewed expected results.
7. A staged deployment, rollback runbook, monitoring, and alert thresholds.

This change does not merge pull requests, deploy services, rotate credentials, or change account/repository permissions.
