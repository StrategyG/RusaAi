# Agent Runtime Readiness Ledger

Status: IMPLEMENTATION IN PROGRESS — NOT PRODUCTION CERTIFIED.

## Implemented in this branch
- Typed task contracts and guarded state machine.
- Async SQLAlchemy persistence for tasks, tenant-scoped memory, and audit.
- Idempotency key per tenant; worker claim lease and retry with backoff.
- Provider-agnostic OpenAI-compatible planner adapter; no fabricated model response.
- Strict registered tool gateway with bounded GitHub repository read/write, feature branch creation, CI check lookup, and draft PR creation.
- Tenant API key mapping resolved server-side; no client-controlled tenant selector.
- Task submit/status/approve/cancel API and independent worker process.
- Plan validation, tool allowlist checks, branch policy, and completion evidence gate.
- Unit tests and dedicated agent CI workflow.

## Not yet certified
- Tests and lint have not been executed in this editing session; CI must pass on the branch.
- API and worker are not yet included in the main serving container/Compose deployment.
- Production database migrations (Alembic), PostgreSQL integration/concurrency tests, backup/restore, and connection pool tuning are outstanding.
- Current audit hash chain is application-level and requires transactional serialization plus immutable external export before tamper-resistance can be claimed.
- Semantic vector memory/RAG, ingestion, ACL-aware retrieval, citation evaluation, retention, and deletion are not implemented.
- No shell/test execution tool is registered. A hardened sandbox is required before enabling code execution.
- Distributed rate limiting, TLS gateway, key rotation, GitHub App least-privilege configuration, OpenTelemetry, metrics, alerts, kill switch, and operator runbooks remain deployment tasks.
- Multi-agent specialist workers currently share a supervisor plan/execution loop; independent role agents and inter-agent message transport require a subsequent iteration.
- Real model endpoint, licensed model identity, benchmark results, target hardware capacity, and SLO evidence must be supplied and evaluated.

## Release rule
Do not merge, release, or deploy until required CI, security review, PostgreSQL isolation tests, model evaluation, load/soak tests, recovery drills, and human owner approval are recorded.
