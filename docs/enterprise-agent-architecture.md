# RUSA-AI Enterprise Agent Architecture

## Architecture decision

Use a hierarchical supervisor with bounded specialist roles and controlled autonomy. The supervisor owns task planning, state transitions, tool authorization, and completion checks. Worker execution is durable and resumable. Full autonomy means allowlisted low-risk actions can run without per-step human input; it never means unrestricted shell, arbitrary filesystem access, secrets, merge, deployment, release, or policy changes.

## Logical topology

    Client / Admin UI
          |
    TLS Gateway / Rate Limit (deployment boundary)
          |
    RUSA Agent API (tenant auth + approver auth)
          |
    Supervisor / Planner ---- Policy + Audit
          |
    PostgreSQL (tasks, leases, messages, memory, audit)
          |
    Worker Pool (separate processes, heartbeat, recovery)
          |
    Tool Registry (JSON Schema validated)
       |          |             |
    GitHub API   MCP Gateway   RAG Tool
                 |             |
          Allowlisted MCP    Embeddings API
          Streamable HTTP       |
                             pgvector index
          |
    Model Platform (OpenAI-compatible chat endpoint)

The model-serving API and agent control plane are separate processes and containers. The agent runtime does not claim that foundation-model weights are included in the repository.

## Agent roles and communication

- Supervisor: decomposes objectives, creates bounded plans, validates tool schemas and branch scope, tracks evidence, and owns task status.
- Analyst: repository inspection and information gathering.
- Architect: design and implementation sequencing.
- Developer: branch-scoped code and documentation writes.
- Reviewer: compares the work branch against the base branch, reviews bounded diff patches against the objective and acceptance criteria, and returns a typed risk assessment. Medium/high/critical findings pause execution for an approver.
- Tester: CI evidence inspection.
- Security: policy, tenant, source ACL, and prompt-injection checks.

The current runtime persists typed WorkerMessage envelopes in agent_messages, including protocol version, message ID, task/correlation IDs, sender, recipient, type, timestamp, and payload. Tool actions are routed to logical roles and reported to the supervisor. The reviewer uses a separate review prompt and typed result contract through the configured model provider; it is a logical specialist, not a separately hosted model. Reviewer rejection pauses the task for a separate approver credential, and any human override is recorded in task evidence. These roles are not yet independently autoscaled agent services; they currently execute within the supervisor-controlled worker process.

## Worker orchestration

Tasks are tenant-scoped and idempotent by tenant_id plus Idempotency-Key. Workers claim queued work using database row locks (SKIP LOCKED on PostgreSQL), renew leases with heartbeats, recover expired leases, retry with exponential backoff, honor cancellation, and persist step cursors and tool evidence. Run the worker separately with python -m rusa_agents.worker_cli.

PostgreSQL is required for multi-replica deployments. SQLite is supported only for local single-process development. Production startup sets RUSA_DB_AUTO_CREATE=false and requires the Alembic migration service to complete before API and worker startup.

## Memory

- Working memory: task plan, cursor, tool results, blockers, approvals.
- Episodic memory: completed task state and evidence.
- Durable key/value memory: tenant-scoped agent_memory with CRUD and keyword search.
- Inter-agent memory: tenant-scoped persisted messages in agent_messages.
- Semantic memory: pgvector-backed document chunks, tenant filters, optional source allowlists, and citation metadata.
- Audit memory: hash-chained event rows; an immutable external export is still required before claiming tamper-proof audit.

## RAG pipeline

The RAG service chunks documents, obtains 384-dimensional embeddings from an OpenAI-compatible embeddings endpoint, and stores chunks in PostgreSQL pgvector with an HNSW cosine index. Retrieval always filters by tenant and can additionally restrict source IDs. Results carry source and chunk IDs for citations. The API supports document ingestion, search, and source deletion.

The current embedding dimension is fixed at 384 by the initial migration. Configure a compatible embedding model via RUSA_EMBEDDING_BASE_URL, RUSA_EMBEDDING_MODEL, and RUSA_EMBEDDING_API_KEY. Keyword memory search is separate from semantic vector RAG.

## MCP gateway

The MCP gateway supports Streamable HTTP servers. Administrators configure server endpoints, optional token environment-variable references, and explicit remote tool allowlists in RUSA_MCP_SERVERS. Only those tools are registered, and each tool's published JSON Schema is validated both when planning and executing. Remote endpoints must use HTTPS except localhost development. The model cannot add servers or change credentials. stdio MCP transport is not currently implemented.

## Tools and safety

Only registered tools execute. Tool arguments are validated against JSON Schema. Registered tool outputs, task context, durable memory, and RAG documents are passed through recursive credential redaction. GitHub reads/writes also block common credential paths. GitHub operations are constrained to the task repository and exact work branch. Code changes require a bounded branch comparison, reviewer result, draft PR, and final CI check on the latest write commit. MCP endpoints and tool names are administrator-configured. RAG retrieval receives tenant identity from trusted worker context, never from model-controlled arguments. No arbitrary shell or filesystem tool is registered.

Approver credentials are separate from tenant API credentials. Merge, deploy, release, secrets, permissions, migrations, and destructive actions remain prohibited or require a future separately reviewed protected-tool implementation.

## API surface

- POST /v1/agent/tasks — submit an idempotent task.
- GET /v1/agent/tasks/{task_id} — tenant-scoped task state and evidence.
- GET /v1/agent/tasks/{task_id}/messages — agent communication trace.
- POST /v1/agent/tasks/{task_id}/approve — approval using X-RUSA-Approver-Key.
- POST /v1/agent/tasks/{task_id}/resume — approver-controlled resume for supported CI/PR blockers.
- POST /v1/agent/tasks/{task_id}/cancel — request cancellation.
- /v1/agent/memory/... — tenant-scoped durable key/value memory.
- /v1/agent/rag/... — tenant-scoped semantic document ingestion, retrieval, and deletion.

## Evaluation

CI checks lint, compilation, contracts, policy, plan validation, task idempotency, tenant isolation, memory, messaging, lease recovery, approver-controlled resume, MCP allowlists, and RAG retrieval ACL behavior. Completion evidence verifies branch, draft PR, and latest-commit CI gates; acceptance criteria are returned for review but are not yet independently evaluated by a dedicated reviewer agent. The production benchmark must additionally report task success, plan validity, tool-call accuracy, policy violation rate, retrieval Recall@k/nDCG, citation precision, CI evidence correctness, latency p50/p95, token/cost per task, and prompt-injection resistance. Every score must identify dataset version, sample size, and evaluator.

## Production gates

- Pass the latest CI and container build/smoke tests.
- Run PostgreSQL migration, concurrency, tenant-isolation, backup/restore, and lease-recovery tests on target infrastructure.
- Validate the configured model and 384-dimensional embedding endpoint; run model and RAG quality benchmarks.
- Review every MCP server, its tool allowlist, authentication, and data-handling terms.
- Deploy behind TLS, distributed rate limiting, secret manager, key rotation, and least-privilege GitHub App credentials.
- Export audit events to immutable/WORM storage and verify the hash chain.
- Add OpenTelemetry traces, metrics, dashboards, queue-depth alerts, dead-letter/recovery operations, kill switch, and incident runbooks.
- Perform security review, adversarial prompt-injection tests, load/soak tests, and disaster-recovery drills.
- Require human owner approval before merge, release, or production deployment.
