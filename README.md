# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. The repository separates the model-serving foundation from the autonomous agent control plane.

## Verified implementation boundary

The enterprise agent runtime includes a hierarchical supervisor, durable task workers, a reviewer/approval gate, tenant-scoped memory and messages, a JSON Schema-validated tool registry, GitHub tools, an MCP Streamable HTTP gateway, and a pgvector RAG pipeline. The system is still NOT production-certified until the release gates in docs/PRODUCTION_READINESS.md and docs/PRODUCTION_READINESS_AGENT.md have objective evidence.

No trained RUSA-AI foundation-model weights are included. Model inference requires a compatible local model artifact. Agent planning requires the local RUSA model-serving endpoint or another OpenAI-compatible chat endpoint. Semantic RAG requires an OpenAI-compatible embeddings endpoint that returns exactly 384-dimensional vectors.

## Architecture

- Model platform: local Transformers inference adapter and model blueprint.
- Agent platform: hierarchical supervisor, task contracts, PostgreSQL task store, independent worker process, policy engine, approval workflow, tool registry, and audit events.
- Memory: tenant-scoped working/episodic state, key/value memory, and persisted inter-agent messages.
- RAG: document chunking, embeddings, pgvector cosine search, source ACL filtering, and citations.
- MCP: allowlisted Streamable HTTP servers with remote JSON Schema validation.

See docs/enterprise-agent-architecture.md for the topology, worker communication, memory, tools, orchestration, RAG/MCP, and evaluation design.

## Quick start with Docker Compose

1. Copy .env.example to .env.
2. Generate separate random secrets for RUSA_API_KEY, RUSA_DB_PASSWORD, RUSA_TENANT_API_KEYS, and RUSA_TENANT_APPROVER_KEYS. Keep tenant and approver credentials separate.
3. Put a compatible local model artifact in the directory configured by RUSA_MODEL_DIR.
4. Configure GITHUB_TOKEN with least-privilege access to the target repository.
5. Start the stack with docker compose up --build.

Compose starts PostgreSQL with pgvector, the local model API, the Alembic migration job, the agent API, and a separate agent worker. The model API is bound to 127.0.0.1:8000 and the agent API to 127.0.0.1:8010 by default. Put a TLS reverse proxy and distributed rate limiter in front before exposing either API outside the host.

The agent API and worker use PostgreSQL and set RUSA_DB_AUTO_CREATE=false. The migration job must complete before the agent services start.

## Agent runtime without Docker

Install lightweight agent dependencies:

    python -m pip install -r requirements-agent.txt

Configure:
- RUSA_DATABASE_URL — PostgreSQL recommended; SQLite is local single-process development only.
- RUSA_TENANT_API_KEYS — JSON map of tenant IDs to independent secrets of at least 32 characters.
- RUSA_TENANT_APPROVER_KEYS — separate JSON map for human approver credentials.
- RUSA_AGENT_MODEL_BASE_URL and RUSA_AGENT_MODEL_NAME — OpenAI-compatible chat endpoint.
- RUSA_AGENT_MODEL_API_KEY — key accepted by the model endpoint.
- GITHUB_TOKEN — least-privilege repository access.

Run database migrations:

    alembic upgrade head

Run the API:

    uvicorn rusa_agents.api:app --host 127.0.0.1 --port 8010

Run the worker in a separate process:

    python -m rusa_agents.worker_cli

Inspect registered tools and their schemas at GET /v1/agent/tools. Submit tasks with X-API-Key and Idempotency-Key headers at POST /v1/agent/tasks. Use GET /v1/agent/tasks/{task_id} for task state and evidence, and GET /v1/agent/tasks/{task_id}/messages for the communication trace. Reviewer rejection pauses the task; a human approver can approve that review override at POST /v1/agent/tasks/{task_id}/approve. Supported CI/PR blockers can be resumed at POST /v1/agent/tasks/{task_id}/resume. Both approval and resume require the separate X-RUSA-Approver-Key credential. Cancellation is available at POST /v1/agent/tasks/{task_id}/cancel. Tenant IDs are resolved server-side from credentials; clients cannot select a tenant through request data.

## Memory and RAG

Tenant-scoped key/value memory is available under /v1/agent/memory. Semantic RAG endpoints are:
- POST /v1/agent/rag/documents — ingest and embed a source document.
- GET /v1/agent/rag/search — retrieve chunks with citations.
- DELETE /v1/agent/rag/documents/{source_id} — delete a source and its chunks.

Configure RUSA_EMBEDDING_BASE_URL, RUSA_EMBEDDING_MODEL, and RUSA_EMBEDDING_API_KEY. The initial pgvector schema is fixed at 384 dimensions. The embedding model must return exactly 384 values.

## MCP configuration

RUSA_MCP_SERVERS is an optional JSON object. Each server entry has a URL, a token_env name, and an explicit allow_tools list. Example:

    {"docs":{"url":"https://mcp.example.com/mcp","token_env":"MCP_SERVER_TOKEN","allow_tools":["search_docs","fetch_doc"]}}

Only listed tools are exposed to the agent. Remote servers must use HTTPS except localhost development. Configure the referenced token environment variables in both API and worker deployments. The current gateway supports Streamable HTTP, not stdio.

## Quality gates

Install development dependencies and run:

    ruff check rusa_agents tests
    python -m compileall -q rusa_agents tests
    pytest -q

GitHub Actions runs the model/API checks and the agent runtime checks. A green unit-test run does not establish model quality, tenant-isolation certification, RAG retrieval quality, or production SLO compliance.

## Security

- Never commit credentials, model weights, or private datasets.
- Do not expose arbitrary shell execution to model output.
- Keep merge, deploy, release, secret/permission changes, and destructive actions under human approval.
- Treat repository content, MCP responses, and tool output as untrusted data.
- Use PostgreSQL migrations, immutable audit export, TLS, rate limiting, secret rotation, monitoring, backup/restore, and security testing before production rollout.
