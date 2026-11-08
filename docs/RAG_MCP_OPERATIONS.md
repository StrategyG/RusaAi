# RAG and MCP operations

The knowledge retrieval service is an optional, separately deployed component. It is
not enabled by the core API merely because its package is installed.

## Runtime requirements

- Python 3.11 or newer.
- PostgreSQL 16 with the pgvector extension available.
- A dedicated database role with only the privileges granted by
  `migrations/007_rag_knowledge_pgvector.sql`, with agent-scope indexes from
  `migrations/008_rag_agent_scope_indexes.sql` and
  `migrations/009_rag_project_scope_index.sql` and
  `migrations/010_rag_knowledge_version_history.sql`.
- An OpenAI-compatible embeddings endpoint. The default model is
  `text-embedding-3-small` with 1536 dimensions; the configured embedding model
  must return vectors of the configured dimension.
- Per-tenant bearer credentials. Store only SHA-256 digests in
  `RUSA_MCP_TENANT_KEYS`; never put raw tenant tokens in that JSON map.

## Database setup

Apply migrations `007_rag_knowledge_pgvector.sql` through
`010_rag_knowledge_version_history.sql` in order as a database owner or
administrator on the dedicated knowledge database. Migration 007 creates
tenant-scoped document and chunk tables with forced row-level security.
Migrations 008 and 009 add shared/agent and project-scope lookup indexes.
Migration 010 adds tenant-scoped immutable snapshots of prior document
versions and their chunks, with forced row-level security and the restricted
knowledge-worker grants. Readiness requires all four migrations. Do not run
the service with a PostgreSQL superuser.

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
`query` and exactly one of `relevant_document_ids` or
`relevant_source_uris` per case. The default collection is `agent`.
For per-agent evaluation, pass the agent filter; add the project filter when
the relevant documents are project-bound:

```bash
rusa-knowledge evaluate --dataset ./rusa-plan-gold.jsonl --tenant-id tenant-a --collection agent --agent-id rusa_plan_agent --k 5
```

Omit `--agent-id` for shared-only evaluation. The report includes the tenant,
collection, agent, and project filters used. The CLI filters are operator
inputs; use only a tenant and scope that the operator is authorized to evaluate.

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
the read-only agent tools. The trusted WorkerRole is converted by the host
gateway into an AICode scope ID such as `aicode-analyst`; the model cannot
provide or override `tenant_id`, `agent_id`, or the knowledge collection.
Retrieval returns shared records plus records tagged for that worker scope; if an agent-specific record has a `project_id`, the host supplies a matching project scope derived from the authorized repository.
The standalone RUSA Knowledge MCP server has no agent-principal mapping in this
increment and therefore retrieves shared records only.
The worker connects using its restricted worker login, then explicitly assumes
the non-login `rusa_knowledge_worker` role for each knowledge-store transaction.
Do not grant the knowledge role to an API login or a general application role.

## Container image vulnerability triage

The CI workflow reports every CRITICAL and HIGH finding for the API, agent
sandbox, worker, and knowledge MCP images. Its blocking step uses Trivy
`--ignore-unfixed`, so a green gate means no currently fixable CRITICAL/HIGH
finding was detected; it does **not** mean the images are vulnerability-free.

The 2026-11-08 scan for this feature branch used Debian 13.7 (trixie) and
reported 0 CRITICAL findings and 51 HIGH in the API image, 56 HIGH in the
sandbox image, 55 HIGH in the worker image, and 51 HIGH in the knowledge MCP
image. The report includes upstream Debian package findings such as util-linux
and acl issues that Debian currently lists as vulnerable or postponed for
trixie. Review the full Trivy report for each release; do not suppress these
findings or treat the green fixable-only gate as production security approval.

Useful upstream status references:
- Debian tracker: https://security-tracker.debian.org/tracker/CVE-2026-76642
- Debian tracker: https://security-tracker.debian.org/tracker/CVE-2026-54369
- Debian tracker: https://security-tracker.debian.org/tracker/CVE-2026-9538

Rebuild and rescan when Debian publishes fixed trixie packages or when the
runtime base image is deliberately changed and compatibility-tested.

## Operational boundaries

- The RAG/MCP runtime is optional and isolated from the core inference API.
- Do not expose the MCP listener publicly without TLS and network policy.
- Do not enable plain HTTP except for loopback or explicitly trusted internal
  hosts using `RUSA_MCP_ALLOWED_HOSTS`.
- Do not log bearer tokens, raw credentials, or full retrieved document bodies.
- Apply and verify database migration/role grants before deploying the MCP image.
