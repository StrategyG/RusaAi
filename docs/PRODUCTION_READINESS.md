# RUSA-AI Production Readiness Evidence Ledger

## Scope

This ledger describes the capabilities present on the integration branch and separates
automated code validation from deployment certification. Passing CI is evidence only
for the exact commit and checks that ran; it is not a production approval.

This ledger covers the repository's AICode subsystem, not certification of the
entire proposed eight-stage Agentic DevOps lifecycle. The current AICode runtime
uses six typed workers and in-process manager dispatch. Separate Plan, Task
Decomposer, Build, Release, Deploy, Operate, and Monitor agents, plus the proposed
external A2A/event-broker topology, remain staged architecture work.

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
- AICode tool failures are classified; only explicitly retryable transient failures receive bounded exponential backoff with full jitter.
- QA evidence is bound to task ID, attempt, worker role, profile, workspace/output digests, execution ID, timestamps, sandbox image, and snapshot size; mixed or incomplete provenance is not accepted.
- Failed QA diagnostics are carried into the repair context; policy and infrastructure failures are escalated instead of being treated as code defects.
- No-op writes are not implementation artifacts. Automatic QA completion requires at least one changed Developer artifact.
- Existing QA baseline files and platform control-plane modules are protected from autonomous Developer writes; new task-local test files may be created and iteratively edited.

## Verification boundary

GitHub Actions passed all three jobs on commit
`3dd2f8e71c2bca8310cffb4327c8b1fe7921838c`: test/lint/security, RAG/MCP
integration, and Docker image build/sandbox/Trivy/Compose validation. The AICode
completion gate now requires all three trusted GitHub Actions jobs to pass on the
same exact commit SHA before a task can be completed.

A local model backend is not a trained RUSA-AI model. No model weights are bundled.
The optional runtime can load a compatible, externally supplied, locally mounted
model artifact after manifest verification. Real-model throughput and quality
remain unmeasured until a specific checkpoint and target hardware are supplied.

## Release gates still open

| Gate | Evidence required | Status |
|---|---|---|
| Integration CI | All jobs green on exact PR head, including Docker/Trivy and RAG/MCP | Passed on 3dd2f8e |
| AICode evidence integrity | Task/attempt/snapshot/image-bound QA evidence and repair diagnostics | Implemented; regression tests pass |
| Agentic DevOps lifecycle | Eight stage agents, Security Guard, and monitor-to-plan feedback loop | Not implemented in current AICode subsystem |
| Local model artifact | Approved model ID/revision, license, tokenizer, pinned manifest digest | Blocked: no artifact supplied |
| Model quality | Versioned task dataset, safety evaluation, human review | Not measured |
| Hardware capacity | p50/p95 latency, throughput, RAM/VRAM, concurrency and soak results | Not measured |
| vLLM production | Compatible CUDA/PyTorch/vLLM versions and target-GPU smoke/load test | Not verified |
| Queue operations | PostgreSQL race, crash recovery, idempotency and cancellation tests | Automated CI tests pass; production database/worker deployment validation remains |
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
- Apply `migrations/007_rag_knowledge_pgvector.sql` only on a pgvector-enabled
  knowledge database and configure the restricted `rusa_knowledge_worker` role.
- Keep MCP private behind TLS/network policy. Never store raw bearer tokens in
  `RUSA_MCP_TENANT_KEYS`; store SHA-256 digests mapped to tenant IDs.
- Do not commit model weights, credentials, tenant bearer tokens, or private
  evaluation datasets.
- Do not describe the complete product as production-ready until every applicable
  release gate has objective evidence and human approval.
