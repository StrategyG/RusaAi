# RUSA-AI Production Readiness Evidence Ledger

**Assessment date:** 2026-11-07  
**Release decision:** **NO-GO — production certification is not yet evidenced.**

This ledger distinguishes repository controls from evidence that must be collected in the target environment. Passing CI, publishing images, or merging a pull request does not certify a live production deployment.

## 1. Current application and platform baseline

- OpenAI-compatible inference API with bounded request/response handling, streaming, admission control, and an optional local Transformers/vLLM backend.
- Local model artifact manifest and SHA-256 verification. No trained RUSA-AI model weights are included in the repository.
- Tenant-scoped PostgreSQL AICode queue with lease tokens, expiry recovery, retry limits, cancellation/completion fencing, and durable task results.
- Tenant-scoped client idempotency: identical retries return the original task; reuse of a key with a different canonical request returns HTTP 409.
- Worker publication fencing: Git publication is performed while the exact task lease row is locked and validated, preventing another worker from reclaiming the task during the bounded publication.
- Optional RAG ingestion and retrieval through PostgreSQL/pgvector, plus a separate read-only bearer-authenticated MCP knowledge service.
- Separate API, worker, MCP, and knowledge-reader database identities; the API cannot read knowledge content, and worker/MCP retrieval is restricted to the read-only knowledge role.
- Non-root API, worker, sandbox, and MCP container build definitions. Production Compose uses an external PostgreSQL service; the RAG/MCP overlay is included only when `RUSA_RAG_ENABLED=true`.
- The proposed eight-stage Agentic DevOps lifecycle is not yet implemented as deployable agents. Current autonomous execution is the AICode worker path; the DevOps Orchestrator, specialist Plan/Code/Build/Test/Release/Deploy/Operate/Monitor agents, and cross-cutting Security Guard remain a separate phased workstream.
- CI coverage for Python tests, PostgreSQL-backed queue/tenant/idempotency tests, image builds, sandbox execution, Trivy vulnerability scanning, production Compose validation, and RAG/MCP integration tests.

## 2. CI evidence snapshot

- PR #49 integrated the local inference runtime, queue reliability, and RAG/MCP foundations into `main`.
- The mainline CI run `42281975019` passed all three jobs on commit `9e0fd2f1abe38026b7d82d74136c38391daea1f1`.
- PR #50 added the post-lease Git publication fence. Its exact head `6a0db58ad474475629acd66b7d9002b8df1e3afe` passed all three CI jobs and was squash-merged as `3c012e0a4342b8e10b974643884539618d222aee`.
- The VPS/RAG-MCP integration branch passed the existing three CI jobs on commit `1232fa5b47ae3089b76ae0a18a7cb23211c8b742` in run `42292637684`. This evidence predates the release/deployment workflow and actionlint additions in this combined branch.
- The combined release-readiness branch still requires its own exact-head CI run, including the new actionlint and deployment-script tests.

CI evidence is valid only for the exact commit and checks that ran. Re-run the complete workflow on every release tag.

## 3. Database migration contract

Apply core migrations in this exact order using a schema owner or migration identity:

1. `migrations/001_aicode_core.sql`
2. `migrations/002_aicode_lease_fencing.sql`
3. `migrations/003_aicode_task_results.sql`
4. `migrations/004_aicode_tenant_rls.sql`
5. `migrations/005_aicode_task_policy_audit.sql`
6. `migrations/006_aicode_idempotency.sql`

The optional knowledge plane requires pgvector and these additional migrations, in order:

7. `migrations/007_rag_knowledge_pgvector.sql`
8. `migrations/008_rag_read_only_reader.sql`

Migration 008 removes the worker's knowledge-ingestion membership from the runtime role, grants a non-login read-only role with RLS enforcement, and removes knowledge-table read access from the API runtime. The worker and MCP service must explicitly assume the read-only role for retrieval. Apply 007–008 only when RAG/MCP is enabled and the restricted roles are configured.

The API and worker must use separate non-superuser login roles, TLS-enabled DSNs, and the least privileges documented in the production runbooks. Runtime identities must never be the schema owner.

## 4. Release and deployment controls added by this change

These controls are implemented in the combined branch and remain unverified until its CI passes and a real tagged release/deployment exercise completes:

- A stable aggregate CI check named **Release Gate**, which succeeds only when test/lint/security, production image, and RAG/MCP jobs all succeed on the same commit.
- All GitHub Actions workflow files are linted with actionlint. Deployment and rollback shell scripts are syntax-checked and exercised with fake Docker/health states.
- A semantic-version release workflow. A tag must match `vMAJOR.MINOR.PATCH` and point to a commit already reachable from `main`.
- The release workflow reruns the complete reusable CI workflow on the exact tag, then builds and publishes four immutable GHCR images: API, worker, sandbox, and RAG/MCP.
- The exact published image digests are scanned for fixable CRITICAL/HIGH vulnerabilities, keylessly signed with GitHub OIDC/Cosign, and given build-provenance and SBOM attestations.
- A release manifest records all four image digests.
- Image publication is gated by the GitHub `release` environment. Production deployment is a separate manual workflow gated by the GitHub `production` environment.
- Deployment verifies all four signatures against the exact release workflow identity and uses SSH with pinned host keys.
- Before deployment, the workflow downloads the signed release's `release-manifest.txt`, verifies the tag/source commit, and requires all four manually supplied digests and image names to match the manifest exactly. The GHCR pull username is validated before it is placed in the remote login command.
- The host deployment script requires existing digest-pinned rollback targets and `RUSA_AICODE_ENABLED=false`; operators must drain active tasks before deployment/rollback.
- The script updates the API/worker/sandbox/MCP digest set, pulls the sandbox through the dedicated rootless Docker socket, includes the RAG/MCP Compose overlay only when `RUSA_RAG_ENABLED=true`, checks required service health, and restores the previous image set if the candidate fails.
- Manual rollback is serialized with deployment and uses the previous environment snapshot.

These workflows do not provision a production host, configure GitHub environment reviewers, create registry credentials, apply database migrations, or deploy anything automatically when this document is updated.

## 5. Release blockers

| Gate | Evidence required | Current status |
|---|---|---|
| Aggregate CI | Release Gate green on the exact combined PR head and release tag | Pending combined-branch CI |
| Branch protection | Active ruleset for `main`; PR-only merges, at least one approval, stale approval dismissal, required Release Gate, no routine bypass | **Not configured**; branch metadata reports `protected: false` |
| Release environment | Required reviewers and tag restrictions on GitHub `release` environment | Not configured/verified |
| Production environment | Required reviewers and deployment restrictions on GitHub `production` environment | Not configured/verified |
| Release pipeline | Successful semantic tag, four GHCR digests, Trivy results, Cosign signatures, provenance, SBOM, release manifest | Not run |
| Deployment host | Provisioned Linux host, rootless Docker socket, SSH access, TLS ingress, firewall, persistent workspace/mirror storage | Not supplied or verified |
| Registry access | Four GHCR packages published and host pull credentials restricted to read-only package access | Not configured/verified |
| Database rollout | Core migrations 001–006 applied on staging; optional 007–008 applied only for RAG/MCP; roles and grants verified | CI tests exist; target staging migration not performed |
| Tenant isolation | Adversarial cross-tenant tests using actual non-superuser API/worker/MCP login identities | Automated tests exist; target-environment review pending |
| Model artifact | Approved model ID/revision, license, tokenizer, manifest, SHA-256 pin, and hardware compatibility | Blocked: no approved artifact supplied |
| Model quality and safety | Versioned evaluation set, quality/safety thresholds, human review, provider/data-processing approval | Not measured/approved |
| Capacity and performance | p50/p95 latency, TTFT/tokens/sec where applicable, throughput, concurrency, RAM/VRAM, and multi-hour soak | Not measured |
| RAG/MCP operations | pgvector database, restricted reader/ingestion roles, tenant key provisioning, TLS/network isolation, ingestion/retrieval acceptance tests | Code and CI coverage exist; production deployment not performed |
| Backup and DR | Encrypted off-host backups, PITR policy, restore drill, approved RPO/RTO, infrastructure-loss exercise | Not completed |
| Observability | Centralized logs, metrics scraping, alert rules, paging test, incident ownership, SLO/error budget | Not completed in target environment |
| Agentic DevOps lifecycle | Eight specialist agents, durable orchestrator state, validated inter-agent contracts/events, Security Guard, human approval boundaries | Not implemented; Phase 1 contract foundation is a separate draft PR |
| Worker/sandbox egress | Deny-by-default outbound allowlist or controlled egress proxy with negative tests | Open blocker; Compose bridge alone is not isolation |
| Security review | Independent threat model/penetration test, secret rotation, TLS verification, least-privilege GitHub token review | Pending |
| Release sign-off | Product owner, SRE/operations, security, and data/model owner approval | Not obtained |

## 6. Model and product scope limitations

- The local inference adapter is a runtime integration, not a trained RUSA-AI model. The repository does not include model weights.
- The default API image does not install heavyweight local-model dependencies. A local-model image requires a separately approved model artifact and target-hardware validation.
- The production Compose stack deploys API and worker services. RAG/MCP is an optional overlay requiring its own DSNs, reader role, tenant tokens, embedding endpoint, and network configuration.
- Identity/JWT and browser workspace capabilities are not included in the current integrated production baseline. If required for the initial product release, they need a separately designed and security-reviewed workstream.
- A Compose configuration check is not proof of high availability, disaster recovery, performance, or operational readiness.

## 7. Production decision rule

Keep `RUSA_AICODE_ENABLED=false` until all applicable P0 gates have objective evidence. Do not enable production tenants merely because CI is green or release images exist.

Production may be declared ready only after:
1. Branch rules and required reviews are active.
2. The exact release tag passes Release Gate and all four published images verify by digest/signature.
3. Staging migrations, runtime-role isolation, tenant isolation, model/provider behavior, and RAG/MCP (if enabled) pass.
4. Staging load/soak, monitoring/alerting, backup/restore, rollback, and disaster-recovery exercises meet owner-approved SLO/RPO/RTO targets.
5. Security, SRE/operations, and product owners sign the release record.

Until then, the correct status is **NO-GO for production certification**.
