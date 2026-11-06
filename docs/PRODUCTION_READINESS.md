# RUSA-AI Production Readiness Evidence Ledger

**Assessment date:** 2026-11-07  
**Release decision:** **NO-GO — production certification is not yet evidenced.**

This ledger separates repository-level controls from evidence that must be collected in the target environment. Passing CI, building containers, or merging a pull request does not certify a live production deployment.

## 1. Capabilities present in the current mainline

- OpenAI-compatible inference API with bounded request/response handling, streaming, admission control, and an optional local Transformers/vLLM backend.
- Local model artifact manifest and SHA-256 verification. No trained RUSA-AI model weights are included in the repository.
- Tenant-scoped PostgreSQL AICode queue with lease tokens, expiry recovery, retry limits, cancellation/completion fencing, and durable task results.
- Tenant-scoped client idempotency: an identical retry returns the original task; reuse of the key with a different canonical request returns HTTP 409.
- Worker publication fencing: Git publication is performed while the exact task lease row is locked and validated, preventing another worker from reclaiming the task during the bounded publication.
- Optional RAG ingestion and retrieval through PostgreSQL/pgvector, plus a separate read-only bearer-authenticated MCP knowledge service.
- Non-root API, worker, sandbox, and MCP container build definitions; production Compose uses immutable API/worker image references and an external PostgreSQL service.
- CI jobs for Python compile/lint/static security, PostgreSQL-backed tests, container builds, sandbox test execution, Trivy vulnerability reporting/blocking, production Compose validation, and RAG/MCP integration tests.

## 2. CI evidence snapshot

- PR #49 integrated the local inference runtime, queue reliability, and RAG/MCP work into `main`.
- The mainline CI run `42281975019` passed all three jobs on commit `9e0fd2f1abe38026b7d82d74136c38391daea1f1`.
- PR #50 added the post-lease Git publication fence. Its exact head `6a0db58ad474475629acd66b7d9002b8df1e3afe` passed all three CI jobs and was squash-merged as `3c012e0a4342b8e10b974643884539618d222aee`.
- The release/deployment workflow additions in the current remediation branch have not yet passed their PR checks or completed a real tagged release. Do not treat them as executed release evidence.

CI evidence is valid only for the exact commit and checks that ran. Re-run the complete CI workflow on every release tag.

## 3. Database migration contract

Apply core migrations in this exact order using a schema owner or migration identity:

1. `migrations/001_aicode_core.sql`
2. `migrations/002_aicode_lease_fencing.sql`
3. `migrations/003_aicode_task_results.sql`
4. `migrations/004_aicode_tenant_rls.sql`
5. `migrations/005_aicode_task_policy_audit.sql`
6. `migrations/006_aicode_idempotency.sql`

Migration `007_rag_knowledge_pgvector.sql` is an optional knowledge-plane migration. Apply it only to a PostgreSQL database with the pgvector extension available and the restricted `rusa_knowledge_worker` role configured. It is not a replacement for core migration 005 or 006.

The API and worker must use separate non-superuser login roles, TLS-enabled DSNs, and the least privileges documented in `docs/aicode-production-runbook.md`. Runtime identities must never be the schema owner.

## 4. Release and deployment controls

The current remediation branch adds the following controls; each remains unverified until the branch checks pass and the first release/deployment exercise completes:

- A stable aggregate CI check named **Release Gate**, which succeeds only when test/lint/security, production image, and RAG/MCP jobs all succeed on the same commit.
- A semantic-version release workflow. A tag must match `vMAJOR.MINOR.PATCH` and point to a commit already reachable from `main`.
- API, worker, and sandbox images are published to GHCR by digest; the exact published digests are rescanned, keylessly signed with GitHub OIDC/Cosign, and given build-provenance attestations and SBOMs.
- A release manifest records the three immutable image digests.
- Production deployment is a separate manual workflow gated by the GitHub `production` environment. It verifies signatures against the exact release workflow identity before using SSH with pinned host keys.
- The deployment script requires existing digest-pinned rollback images and `RUSA_AICODE_ENABLED=false`; operators must drain active tasks before deployment. It updates the API/worker/sandbox digest set, pulls the sandbox image through the dedicated rootless Docker socket, waits for API and worker health, and restores the prior image set if candidate health checks fail.
- Manual rollback is serialized with deployment and uses the previous environment snapshot.

These workflows do not provision a production host, configure GitHub environment reviewers, create registry credentials, apply database migrations, or deploy anything automatically when this document is updated.

## 5. Release blockers

| Gate | Evidence required | Current status |
|---|---|---|
| Aggregate CI | Release Gate green on the exact PR head and release tag | Pending remediation-branch CI |
| Branch protection | Active ruleset for `main`; PR-only merges, at least one approval, stale approval dismissal, required Release Gate, no routine bypass | **Not configured**; branch metadata currently reports `protected: false` |
| Release pipeline | Successful semantic tag, GHCR digests, Trivy results, Cosign signatures, provenance, SBOM, and release manifest | Not run |
| Production environment | Required reviewers and deployment restrictions on GitHub `production` environment | Not configured/verified |
| Deployment host | Provisioned Linux host, rootless Docker socket, SSH access, TLS ingress, firewall, persistent workspace/mirror storage | Not supplied or verified |
| Registry access | GHCR packages published and host pull credentials restricted to read-only package access | Not configured/verified |
| Database rollout | Core migrations 001–006 applied on staging; optional 007 only if RAG is enabled; role grants and startup checks verified | Not performed |
| Tenant isolation | Adversarial cross-tenant tests using the actual non-superuser API/worker login identities | Automated tests exist; target-environment review pending |
| Model artifact | Approved model ID/revision, license, tokenizer, manifest, SHA-256 pin, and hardware compatibility | Blocked: no approved artifact supplied |
| Model quality and safety | Versioned evaluation set, quality/safety thresholds, human review, provider/data-processing approval | Not measured/approved |
| Capacity and performance | p50/p95 latency, TTFT/tokens/sec where applicable, throughput, concurrency, RAM/VRAM, and multi-hour soak | Not measured |
| RAG/MCP operations | pgvector database, restricted role, tenant key provisioning, TLS/network isolation, ingestion/retrieval acceptance tests | Code and CI coverage exist; production deployment not performed |
| Backup and DR | Encrypted off-host backups, PITR policy, restore drill, approved RPO/RTO, infrastructure-loss exercise | Not completed |
| Observability | Centralized logs, metrics scraping, alert rules, paging test, incident ownership, SLO/error budget | Not completed in target environment |
| Security review | Independent threat model/penetration test, secret rotation, TLS verification, least-privilege GitHub token review | Pending |
| Release sign-off | Product owner, SRE/operations, security, and data/model owner approval | Not obtained |

## 6. Model and product scope limitations

- The local inference adapter is a runtime integration, not a trained RUSA-AI model. The repository does not include model weights.
- The default API image does not install heavyweight local-model dependencies. A local-model image requires a separate approved build and target-hardware validation.
- The production Compose stack deploys API and worker services. RAG/MCP is optional and requires its own deployment/network/database configuration.
- Identity/JWT and browser workspace capabilities are not included in the current integrated production baseline. If they are required for the initial product release, they need a separately designed and security-reviewed workstream.
- A Compose configuration check is not proof of high availability, disaster recovery, performance, or operational readiness.

## 7. Production decision rule

Keep `RUSA_AICODE_ENABLED=false` until all applicable P0 gates have objective evidence. Do not enable production tenants merely because CI is green or a release image exists.

Production may be declared ready only after:
1. Branch rules and required reviews are active.
2. The exact release tag passes Release Gate and all published images verify by digest/signature.
3. Staging migrations, runtime-role isolation, tenant isolation, model/provider behavior, and RAG/MCP (if enabled) pass.
4. Staging load/soak, monitoring/alerting, backup/restore, rollback, and disaster-recovery exercises meet owner-approved SLO/RPO/RTO targets.
5. Security, SRE/operations, and product owners sign the release record.

Until then, the correct status is **NO-GO for production certification**.
