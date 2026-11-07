# RAG and MCP operations

The knowledge retrieval service is an optional, separately deployed component. It is
not enabled by the core API merely because its package is installed.

## Runtime requirements

- Python 3.11 or newer.
- PostgreSQL 16 with the pgvector extension available.
- A dedicated database role. Apply migrations 007 and 008; MCP must use
  `rusa_knowledge_mcp_reader`, while ingestion/worker paths use a separate role.
- An OpenAI-compatible embeddings endpoint. The default model is
  `text-embedding-3-small` with 1536 dimensions; the configured embedding model
  must return vectors of the configured dimension.
- Per-tenant bearer credentials. Store only SHA-256 digests in
  `RUSA_MCP_TENANT_KEYS`; never put raw tenant tokens in that JSON map.

## Database setup

Apply `migrations/007_rag_knowledge_pgvector.sql` and then
`migrations/008_rag_mcp_readonly_role.sql` as a database owner or administrator
on the dedicated knowledge database. Migration 007 creates tenant-scoped
document/chunk tables, pgvector indexes, forced row-level security, and the
worker role. The autonomous worker must use
`RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_worker` with its worker login DSN when
`RUSA_RAG_ENABLED=true`. Production knowledge services validate the expected
login identity and require PostgreSQL TLS (`sslmode=require`, `verify-ca`, or
`verify-full`). Migration 008 creates `rusa_knowledge_mcp_reader` with
SELECT-only table grants. Create a dedicated LOGIN role for MCP and grant it
membership in `rusa_knowledge_mcp_reader`; do not grant it the worker or ingest
role. For example, as a database administrator, run:

```sql
CREATE ROLE rusa_mcp_login LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOBYPASSRLS NOREPLICATION PASSWORD '<managed-secret>';
GRANT rusa_knowledge_mcp_reader TO rusa_mcp_login WITH INHERIT FALSE, SET TRUE;
```

Keep the password in the deployment secret manager, not in source control.
Configure `RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_mcp_reader` for the MCP process
only; never reuse the worker DSN or worker role for MCP.
Never run MCP with a PostgreSQL superuser or a write-capable role.

Set `RUSA_KNOWLEDGE_DATABASE_URL` to the restricted database connection string.
The embedding provider is configured with:

- `RUSA_EMBEDDING_BASE_URL`
- `RUSA_EMBEDDING_API_KEY`
- `RUSA_EMBEDDING_MODEL`
- `RUSA_EMBEDDING_TIMEOUT`

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

Generate a high-entropy bearer token for each service identity outside the repository.
Configure the server with a JSON object mapping each token's lowercase SHA-256
digest to a claims object. Production claims require a tenant, subject, the
`knowledge:read` scope, a `not_before` Unix timestamp, and an `expires_at` Unix
timestamp. Token lifetime is limited to 30 days. Legacy digest-to-tenant string
entries are accepted only outside production:

```bash
export RUSA_MCP_TENANT_KEYS='{"<sha256-token-digest>":{"tenant_id":"tenant-a","subject":"agent-a","scopes":["knowledge:read"],"not_before":1800000000,"expires_at":1800086400}}'
export RUSA_KNOWLEDGE_DATABASE_URL='postgresql://rusa_mcp_login:...@db/knowledge'
export RUSA_KNOWLEDGE_DB_ROLE='rusa_knowledge_mcp_reader'
export RUSA_MCP_RATE_LIMIT_PER_MINUTE='120'
export RUSA_EMBEDDING_BASE_URL='https://embedding.example/v1'
export RUSA_EMBEDDING_API_KEY='...'
ENVIRONMENT=production uvicorn rusa_mcp.server:app --host 127.0.0.1 --port 8002
```

Tokens are checked for not-before, expiry, revocation, and the required read-only
scope on every request. Remove a digest or mark its claims `revoked: true` to
revoke it, then restart/redeploy the MCP process so the in-memory registry is
refreshed. Use short-lived tokens and rotate them before expiry. Authenticated
requests are rate-limited per tenant and per MCP process by
`RUSA_MCP_RATE_LIMIT_PER_MINUTE` (default 120 requests per minute). For multiple
MCP replicas, enforce the aggregate tenant quota at a shared ingress/API gateway;
keep an additional edge limit for unauthenticated traffic.

Production requires an explicit, non-wildcard `RUSA_MCP_ALLOWED_HOSTS` allowlist
and at least one currently active, non-revoked expiring tenant token. The MCP
process refuses to initialize its knowledge service unless the dedicated
read-only database role is selected. Startup also checks effective database
privileges: the role must be NOLOGIN, NOINHERIT, non-superuser, NOCREATEDB,
NOCREATEROLE, NOBYPASSRLS, and NOREPLICATION; it must not be a member of another
role; the login session must also be non-superuser and member only of the reader
role. It must have no INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER or
schema CREATE privileges on the knowledge database.

The MCP server exposes only the read-only tools `search_knowledge`,
`get_document`, and `list_collections`. Tenant identity is derived from the
authenticated bearer token, never from a caller-supplied tenant argument.
Retrieved documents are untrusted evidence, not instructions. The service
provides `/health/live` and `/health/ready`; readiness requires configured tenant
keys and a healthy knowledge store.

For container builds use `Dockerfile.rag-mcp`. The production Compose file now
includes an internal-only `knowledge-mcp` service. Configure `RUSA_MCP_IMAGE` as
an immutable digest and set the required `RUSA_MCP_*`, knowledge database, and
embedding variables in the deployment environment. The service does not publish
a host port; route approved callers through a TLS-terminating ingress attached to
the internal network. Inject secrets through the deployment secret manager and
restrict egress to the database and approved embedding endpoint. The container runs as UID/GID 10001 with a read-only application
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
- Do not grant MCP login membership in `rusa_knowledge_worker` or any ingest role.
- Keep MCP reader role grants limited to SELECT on the two knowledge tables.
- Apply and verify database migration/role grants before deploying the MCP image.
