# Agent Runtime Readiness Ledger

Status: IMPLEMENTATION ADVANCED — NOT PRODUCTION CERTIFIED.

## Implemented in the feature branch
- Typed task contracts and guarded task-state transitions.
- Async SQLAlchemy persistence for tasks, tenant-scoped memory, agent messages, and audit events.
- Tenant-scoped idempotent task submission, worker leases, heartbeat renewal, expired-lease recovery, retries, cancellation, approver-controlled resume for supported blockers, and persisted step cursors.
- OpenAI-compatible planner adapter; no fabricated model completions.
- Hierarchical supervisor, logical specialist-role routing, persisted message bus, and plan validation.
- JSON Schema-validated tool registry with GitHub branch-scoped operations.
- Separate tenant and approver API credentials.
- MCP Streamable HTTP gateway with administrator-defined server/tool allowlists and schema validation.
- Tenant-scoped pgvector RAG ingestion, retrieval, source filtering, citations, and deletion; embedding dimension fixed at 384.
- Tenant-scoped key/value memory CRUD and keyword search.
- Agent API, separate worker process, lightweight agent image, PostgreSQL/pgvector Compose service, and Alembic initial migration.
- Unit tests covering task contracts, policy, tenant isolation, message persistence, lease recovery, MCP allowlists, and RAG source ACL behavior.

## Not yet certified
- Latest CI for the current head must pass; code written to GitHub is not evidence of a passing build.
- PostgreSQL migration and concurrency tests must be executed against the actual pgvector container and production-like settings.
- The main model-serving image still requires an externally supplied compatible local model artifact.
- RAG requires an externally supplied OpenAI-compatible embeddings endpoint that returns exactly 384-dimensional vectors.
- MCP requires separately reviewed, reachable HTTPS servers and credentials; only Streamable HTTP is supported.
- Logical specialist roles share the supervisor worker process; independently scaled agent services and distributed message transport remain future work.
- Audit hash chaining is not a substitute for transactional chain serialization and immutable external/WORM export.
- Distributed rate limiting, TLS ingress, secret-manager integration, credential rotation, GitHub App least-privilege installation, OpenTelemetry, dashboards, alerts, kill switch, and operator runbooks remain deployment requirements.
- No arbitrary shell/test execution is registered. A hardened sandbox and separate approval design are required before adding execution tools.
- Retrieval benchmarks, model evaluations, load/soak tests, penetration tests, backup/restore drills, and SLO evidence remain outstanding.

## Release rule

Do not merge, release, or deploy until the latest CI and image build pass, PostgreSQL/pgvector integration and tenant-isolation tests are recorded, model/RAG/MCP configuration is validated, security and operational reviews are complete, and the repository owner approves the release.
