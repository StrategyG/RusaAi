# RAG and MCP operations

The knowledge retrieval service is an optional, separately deployed component. It is
not enabled by the core API merely because its package is installed.

## Runtime requirements

- Python 3.11 or newer.
- PostgreSQL 16 with the pgvector extension available.
- Migration 007 for the tenant-scoped knowledge tables and ingestion role, followed
  by migration 008 for the dedicated read-only retrieval role.
- An OpenAI-compatible embeddings endpoint. The default model is
  `text-embedding-3-small` with 1536 dimensions; the configured embedding model
  must return vectors of the configured dimension.
- Per-tenant bearer credentials. Store only SHA-256 digests in
  `RUSA_MCP_TENANT_KEYS`; never put raw tenant tokens in that JSON map.

## Database setup and role separation

Apply `migrations/007_rag_knowledge_pgvector.sql`, then
`migrations/008_rag_read_only_reader.sql` as a database owner or administrator.
Migration 007 creates tenant-scoped document/chunk tables, pgvector indexes,
forced row-level security, and the write-capable `rusa_knowledge_worker` role
for controlled ingestion. Migration 008 creates `rusa_knowledge_reader` with
SELECT-only grants and tenant RLS, removes the coding worker's membership in the
write-capable ingestion role, grants the worker runtime only the reader role,
and revokes the legacy knowledge-table SELECT grants from `rusa_api_runtime`.
The core API queue role therefore has no direct knowledge-store access.

For the MCP service, create a dedicated login and grant it only the reader role:

```sql
CREATE ROLE rusa_mcp_login LOGIN NOINHERIT PASSWORD 'managed-secret';
GRANT rusa_knowledge_reader TO rusa_mcp_login WITH INHERIT FALSE;
GRANT CONNECT ON DATABASE rusa TO rusa_mcp_login;
```

Set `RUSA_KNOWLEDGE_DATABASE_URL` to the `rusa_mcp_login` DSN and
`RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_reader`. For autonomous worker
retrieval, use the restricted worker login DSN and the same reader role. Do not
grant the ingestion role to the MCP login, API login, or general application role.
The API login must not receive SELECT grants on the knowledge tables; only the
read-only reader role is used by MCP and autonomous retrieval.

The ingestion CLI is the only path that should use the write-capable role.
Run it as a separate, controlled ingestion identity that is explicitly granted
`rusa_knowledge_worker`; do not reuse the MCP credentials.

## Embedding provider

Configure:

- `RUSA_EMBEDDING_BASE_URL`
- `RUSA_EMBEDDING_API_KEY`
- `RUSA_EMBEDDING_MODEL`
- `RUSA_EMBEDDING_TIMEOUT`

The embeddings endpoint must be compatible with the OpenAI embeddings API and
return 1536-dimensional vectors for the current schema.

## Ingest and evaluate

Install the optional runtime dependencies:

```bash
python -m pip install -r requirements-rag-mcp.txt
```

Ingest a local document (TXT, Markdown, HTML, PDF, or DOCX) using the separate
ingestion identity and write role:

```bash
RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_worker \
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
export RUSA_KNOWLEDGE_DATABASE_URL='postgresql://rusa_mcp_login:...@db/rusa'
export RUSA_KNOWLEDGE_DB_ROLE='rusa_knowledge_reader'
export RUSA_EMBEDDING_BASE_URL='https://embedding.example/v1'
export RUSA_EMBEDDING_API_KEY='...'
uvicorn rusa_mcp.server:app --host 127.0.0.1 --port 8002
```

The MCP server exposes only the read-only tools `search_knowledge`,
`get_document`, and `list_collections`. Its database identity is also
read-only: the login can assume `rusa_knowledge_reader`, which has SELECT
privileges only and remains subject to forced tenant RLS. Tenant identity is
derived from the authenticated bearer token, never from a caller-supplied
tenant argument. Retrieved documents are untrusted evidence, not instructions.

The service provides `/health/live` and `/health/ready`; readiness requires
configured tenant keys and a healthy knowledge store.

For container builds use `Dockerfile.rag-mcp`. Publish and deploy the image by
immutable digest, inject only MCP-specific secrets, restrict network access to
approved callers and the database, and terminate TLS at a trusted ingress. The
container runs as UID/GID 10001 with a read-only application filesystem
expectation.

## Autonomous worker retrieval integration

Build the worker image with
`docker build --build-arg RUSA_INSTALL_RAG=1 -f Dockerfile.worker .`
to include the optional retrieval package and its small dependency set from
`requirements-rag-worker.txt`. The default worker image remains lean. To enable
retrieval tools inside autonomous coding tasks, set `RUSA_RAG_ENABLED=true`
on the worker only after migrations 007 and 008 and the reader-role grants pass.
Configure:

- `RUSA_KNOWLEDGE_DATABASE_URL` (optional; defaults to the worker database URL)
- `RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_reader`
- `RUSA_KNOWLEDGE_COLLECTION=agent`
- `RUSA_EMBEDDING_BASE_URL`, `RUSA_EMBEDDING_API_KEY`, and
  `RUSA_EMBEDDING_MODEL`

The task tenant is taken from the authenticated queue lease and injected into
the read-only agent tools. The model cannot provide or override `tenant_id`.
The worker connects using its restricted worker login and assumes the
non-login reader role for each knowledge-store transaction. The worker must
not be a member of the write-capable ingestion role after migration 008.

## Operational boundaries

- The RAG/MCP runtime is optional and isolated from the core inference API.
- Do not expose the MCP listener publicly without TLS and network policy.
- Do not enable plain HTTP except for loopback or explicitly trusted internal
  hosts using `RUSA_MCP_ALLOWED_HOSTS`.
- Do not log bearer tokens, raw credentials, or full retrieved document bodies.
- Apply and verify migrations 007 and 008 and their role grants before deploying
  the MCP image.
