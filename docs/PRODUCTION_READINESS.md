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

The primary CI job has previously passed compile, lint, Bandit, and unit tests on
commit `7ed80f0801651165cd634b95dcd993a6c85b9484`. Subsequent commits added
RAG/MCP packaging, queue idempotency, race tests, and further CI changes; those
changes must be judged only by the latest workflow run on the exact PR head.

A local model backend is not a trained RUSA-AI model. No model weights are bundled.
The optional runtime can load a compatible, externally supplied, locally mounted
model artifact after manifest verification. Real-model throughput and quality
remain unmeasured until a specific checkpoint and target hardware are supplied.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| Integration CI | All jobs green on exact PR head, including Docker/Trivy and RAG/MCP | Pending latest run |
| Local model artifact | Approved model ID/revision, license, tokenizer, pinned manifest digest | Blocked: no artifact supplied |
| Model quality | Versioned task dataset, safety evaluation, human review | Not measured |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency and soak results | Not measured |
| vLLM production | Compatible CUDA/PyTorch/vLLM versions and target-GPU smoke/load test | Not verified |
| Queue operations | PostgreSQL race, crash recovery, idempotency and cancellation tests | Automated tests added; latest run pending |
| RAG/MCP deployment | Apply pgvector migration, restricted DB role, TLS/network controls, tenant-key provisioning | Code and tests added; deployment not performed |
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
- Apply migrations `001` through `006` in order using the database owner before
  enabling production task intake. The API runtime must not have schema-owner or
  superuser privileges.
- Apply `migrations/005_rag_knowledge_pgvector.sql` only on a pgvector-enabled
  knowledge database and configure the restricted `rusa_knowledge_worker` role.
- Keep MCP private behind TLS/network policy. Never store raw bearer tokens in
  `RUSA_MCP_TENANT_KEYS`; store SHA-256 digests mapped to tenant IDs.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- Do not describe the complete product as production-ready until every applicable
  release gate has objective evidence and human approval.


## Production hardening candidate — PR #106

The current hardening candidate adds the following code-level controls:

- Production API and worker PostgreSQL DSNs require `sslmode=verify-full`.
- Production Compose no longer bulk-injects `.env` into API and worker containers; each service receives an explicit environment allowlist.
- `python -m rusa_agents.production_preflight` validates runtime configuration, the web session secret, and exact repository-owned GHCR image references pinned by SHA-256 digest.
- Web CI audits production dependencies and scans the final web container for CRITICAL/HIGH vulnerabilities.
- Regression tests cover TLS identity verification, service-secret separation, and immutable image references.

The preflight validates configuration and digest syntax only. It does not verify Cosign signatures, SLSA provenance, SBOM contents, deployment state, or external infrastructure.

## Additional production gates observed on 2026-11-11

| Gate | Current evidence | Required action |
|---|---|---|
| Main branch governance | `main` reports `protected=false`; no repository rulesets were returned | Configure required reviews/status checks and block force-push/deletion |
| Signed release pipeline | PR #53 remains open; its described release images cover API, worker, and sandbox | Complete the release workflow and bind the web image to the same signed manifest/provenance policy |
| Certification CLI | PR #72 remains open; PR #71 contract layer is merged | Review and integrate the CLI and its exact-manifest evaluation path |
| Web dependency reproducibility | CI resolves a lockfile artifact; no committed `web/package-lock.json` exists | Commit the lockfile and use `npm ci` in CI and Docker builds |
| Vulnerability disposition | Current blocking Trivy gate ignores unfixed findings | Fix or formally approve every remaining CRITICAL/HIGH finding against the exact image digest |
| Agentic DevOps lifecycle | PRs #99 and #100 remain drafts | Complete review, integration, live worker-pool and staging evidence |
| RusaModels integration | PR #103 remains draft and describes a v0.1.0-draft contract | Finalize and implement the model-plane contract before relying on it in production |
| Operational certification | No live tenant-isolation, sandbox adversarial, backup/restore, load/soak, or model-quality evidence was supplied | Run the required staging exercises and obtain operator sign-off |

Do not describe the full RUSA AI product as production-ready until the applicable gates above have objective evidence and human approval.
