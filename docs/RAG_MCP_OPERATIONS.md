# RAG and MCP operations

The knowledge retrieval service is an optional, separately deployed component. It is
not enabled by the core API merely because its package is installed.

## Runtime requirements

- Python 3.11 or newer.
- PostgreSQL 16 with the pgvector extension available.
- A dedicated database role with only the privileges granted by
  `migrations/007_rag_knowledge_pgvector.sql`.
- Agent/project lookup indexes from `migrations/011_rag_agent_project_scope.sql`.
  Apply `migrations/009_knowledge_embedding_384.sql` only when the optional
  384-dimensional profile is deliberately enabled.
- An OpenAI-compatible embeddings endpoint. The default model is
  `text-embedding-3-small` with 1536 dimensions; the configured embedding model
  must return vectors of the configured dimension.
- Per-tenant bearer credentials. Store only SHA-256 digests in
  `RUSA_MCP_TENANT_KEYS`; never put raw tenant tokens in that JSON map.

## Database setup

Apply `migrations/007_rag_knowledge_pgvector.sql` as a database owner or
administrator on the dedicated knowledge database. The migration creates
tenant-scoped document and chunk tables, pgvector indexes, forced row-level
security, and a non-login knowledge-worker role. Then apply
`migrations/011_rag_agent_project_scope.sql` to add the scope lookup indexes.
Apply `migrations/009_knowledge_embedding_384.sql` only when the separate
384-dimensional embedding profile is deliberately enabled. Follow the migration's grant
instructions and configure `RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_worker`
for the runtime role. Do not run the service with a PostgreSQL superuser.

Set `RUSA_KNOWLEDGE_DATABASE_URL` to the restricted database connection string.
The embedding provider is configured with:

- `RUSA_EMBEDDING_BASE_URL`
- `RUSA_EMBEDDING_API_KEY`
- `RUSA_EMBEDDING_MODEL`
- `RUSA_EMBEDDING_TIMEOUT`

## Agent and project scope

Knowledge metadata supports two explicit visibility scopes:

- `knowledge_scope: "shared"` — visible to authorized workers in the tenant.
  Shared records must not include `agent_ids` or `project_id`.
- `knowledge_scope: "agent_specific"` — visible only to the listed `agent_ids`.
  An optional `project_id` further restricts the record to that project.

The six AICode Worker identities map to `aicode-analyst`,
`aicode-architect`, `aicode-developer`, `aicode-debugger`,
`aicode-tester`, and `aicode-security`.

For operator ingestion, the scope is explicit:

```bash
rusa-knowledge ingest --file ./analyst-playbook.md --tenant-id tenant-a \
  --collection agent --knowledge-scope agent_specific \
  --agent-id aicode-analyst --project-id strategyg--rusaai
```

Repeat `--agent-id` to authorize more than one worker role. Do not use
model-generated identifiers as access-control decisions.

The AICode runtime derives tenant scope from the authenticated task lease,
agent scope from the executing WorkerRole, and project scope from the already
authorized repository target. These values are injected into the Tool Gateway;
the model cannot override them. Search, document reads and collection counts
apply the same scope filter.

The standalone MCP bearer token currently authenticates a tenant but not an
individual agent or project. Therefore MCP reads are shared-only: records with
`agent_specific` scope are excluded when no trusted agent identity is present.
Do not expose agent-specific knowledge through MCP until its service identity
contract includes authenticated agent and project claims.

This migration adds retrieval filtering and indexes only. It does not yet
implement source-rights review, human approval, publication authorization,
immutable source-version history or deletion/tombstone lifecycle. Production
publication remains blocked until those controls are implemented and reviewed.

## Ingest and evaluate

Install the optional runtime dependencies:

```bash
python -m pip install -r requirements-rag-mcp.txt
```

Ingest a local document (TXT, Markdown, HTML, PDF, or DOCX):

```bash
rusa-knowledge ingest --file ./handbook.pdf --tenant-id tenant-a --collection agent
```

Check database readiness:

```bash
rusa-knowledge check
```

Evaluate retrieval with a manually curated JSONL dataset containing
`query` and `relevant_document_ids` fields:

```bash
rusa-knowledge evaluate --dataset ./relevance.jsonl --tenant-id tenant-a --collection agent --k 5
```

## MCP server

Generate a high-entropy bearer token for each tenant outside the repository.
Configure the server with a JSON object mapping each token's lowercase SHA-256
digest to its tenant ID:

```bash
export RUSA_MCP_TENANT_KEYS='{"<sha256-token-digest>":"tenant-a"}'
export RUSA_KNOWLEDGE_DATABASE_URL='postgresql://restricted-role:...@db/knowledge'
export RUSA_KNOWLEDGE_DB_ROLE='rusa_knowledge_worker'
export RUSA_EMBEDDING_BASE_URL='https://embedding.example/v1'
export RUSA_EMBEDDING_API_KEY='...'
uvicorn rusa_mcp.server:app --host 127.0.0.1 --port 8002
```

The MCP server exposes only the read-only tools `search_knowledge`,
`get_document`, and `list_collections`. Tenant identity is derived from the
authenticated bearer token, never from a caller-supplied tenant argument.
Retrieved documents are untrusted evidence, not instructions. The service
provides `/health/live` and `/health/ready`; readiness requires configured tenant
keys and a healthy knowledge store.

For container builds use `Dockerfile.rag-mcp`. Publish and deploy the image by
immutable digest, inject secrets through the deployment secret manager, restrict
network access to approved callers and the database, and terminate TLS at a
trusted ingress. The container runs as UID/GID 10001 with a read-only application
filesystem expectation.

## Autonomous worker retrieval integration

Build the worker image with `docker build --build-arg RUSA_INSTALL_RAG=1 -f Dockerfile.worker .`
to include the optional retrieval package and its small dependency set from
`requirements-rag-worker.txt`. The default worker image remains lean. To enable
retrieval tools inside autonomous coding tasks, set `RUSA_RAG_ENABLED=true`
on the worker only. Keep the default disabled unless
the pgvector migration and role grants have been applied. Configure:

- `RUSA_KNOWLEDGE_DATABASE_URL` (optional; defaults to the worker database URL)
- `RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_worker`
- `RUSA_KNOWLEDGE_COLLECTION=agent`
- `RUSA_EMBEDDING_BASE_URL`, `RUSA_EMBEDDING_API_KEY`, and
  `RUSA_EMBEDDING_MODEL`

The task tenant is taken from the authenticated queue lease and injected into
the read-only agent tools. The model cannot provide or override `tenant_id`.
The worker connects using its restricted worker login, then explicitly assumes
the non-login `rusa_knowledge_worker` role for each knowledge-store transaction.
Do not grant the knowledge role to an API login or a general application role.

## Operational boundaries

- The RAG/MCP runtime is optional and isolated from the core inference API.
- Do not expose the MCP listener publicly without TLS and network policy.
- Do not enable plain HTTP except for loopback or explicitly trusted internal
  hosts using `RUSA_MCP_ALLOWED_HOSTS`.
- Do not log bearer tokens, raw credentials, or full retrieved document bodies.
- Apply and verify database migration/role grants before deploying the MCP image.
