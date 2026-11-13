# RAG and MCP operations

The knowledge retrieval service is an optional, separately deployed component. It is
not enabled by the core API merely because its package is installed.

## Canonical shared knowledge-store decision

ADR-0002 selects the existing RusaAi `rusa_knowledge` PostgreSQL/pgvector service
as the canonical shared Agent/Worker knowledge store, subject to the release gates
below. This is an architecture decision, not evidence that a production database
has been migrated, populated, deployed or activated.

- SQLite/FTS5 remains device-local memory/state and local retrieval. It is not a
  second writer or a replica of the shared corpus.
- RAG_Cerdas IM19 remains a separate candidate system. Do not dual-write, mirror,
  bulk-copy or cross-read it with RusaAi under this decision.
- RusaModels is accessed through an authenticated Model Plane API for inference
  and embedding; RusaAi must not access its database or model-storage credentials.
- The 1,536-dimensional and 384-dimensional embeddings remain separate vector
  spaces. The final production embedding profile is not selected by ADR-0002.
- The service remains optional and disabled by default until source rights,
  schema/migration, ACL, retrieval evaluation, deployment and owner approval pass.

See `docs/adr/ADR-0002-canonical-shared-agent-worker-knowledge-store.md` for
the decision, alternatives and implementation/release gates.


## Runtime requirements

- Python 3.11 or newer.
- PostgreSQL 16 with the pgvector extension available.
- A dedicated database role with only the privileges granted by
  `migrations/007_rag_knowledge_pgvector.sql`.
- Agent/project lookup indexes from `migrations/011_rag_agent_project_scope.sql`.
- Append-only document version history from
  `migrations/012_rag_knowledge_version_history.sql`.
- Rights, review, approval, audit and publication gates from
  `migrations/013_rag_knowledge_governance.sql`.
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
`migrations/011_rag_agent_project_scope.sql` to add the scope lookup indexes
and `migrations/012_rag_knowledge_version_history.sql` to create append-only
document/version chunk snapshots. Then apply
`migrations/013_rag_knowledge_governance.sql` to create the rights, review,
approval, audit and publication gate. Apply
`migrations/009_knowledge_embedding_384.sql` only when the separate
384-dimensional embedding profile is deliberately enabled. Migration 012
stores both 1536d and 384d version-vector columns; the runtime writes only
the configured embedding space. Follow the migration's grant
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

Migration 011 adds agent/project retrieval filtering and lookup indexes.
Migration 012 adds append-only document/version chunk snapshots. Migration 013
adds source-rights attestations, technical/source/security/QA review findings,
separate human approvals, append-only audit events and a database-enforced
publication/withdrawal gate. Candidate versions are available only through
the reviewer-only path; answer-facing retrieval defaults to published
versions only.

The implementation still does not provide a canonical source registry and
source-byte snapshot service, signed manifest verification, index-generation
or external vector-store reconciliation, cache/backup tombstone propagation,
or a privileged tenant-erasure/retention workflow. Production remains NO-GO
until those remaining controls and the independent review gates are complete.

## Rights, review, approval and publication lifecycle

Answer-facing retrieval is publication-gated. The runtime defaults
`RUSA_KNOWLEDGE_REQUIRE_PUBLICATION=true`; candidate documents without a
current published version are not returned by search, document reads,
collection counts or version-history reads. Do not disable this setting in a
production environment. The service rejects the false setting whenever
`ENVIRONMENT=staging` or `ENVIRONMENT=production`; false is reserved for
disposable tests and isolated development.

The governance migration creates separate non-login database roles:

- `rusa_knowledge_reviewer` — may append rights attestations and technical,
  source-fidelity, security and QA review findings.
- `rusa_knowledge_approver` — may append human approval/rejection/revocation
  decisions.
- `rusa_knowledge_publisher` — may invoke the publication/withdrawal functions
  after the database verifies the complete gate.

A DBA must grant each human's distinct database login membership in exactly
the required no-login role. Do not share one login across reviewer, approver
and publisher. Database triggers overwrite caller-supplied actor columns with
the authenticated `session_user` and reject inserts from the wrong role.
All governance actors must be least-privileged LOGIN roles: no SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB, REPLICATION or NOINHERIT. The database rechecks the complete direct and transitive membership closure for every governance event and publication/withdrawal call; each login must inherit only its assigned no-login governance role, with no additional group memberships or admin option.
Audit rows also record the database `session_user` and active role;
actor identity is not accepted from model output or CLI arguments.

Answer-facing document reads reconstruct content from immutable version chunks
and take title, source URI, collection and scope metadata from the immutable
version record. Search accepts active index rows only when their collection,
content and metadata match that version snapshot. If the configured embedding
vector is pinned in the snapshot, the active index vector must match it; an
unpopulated dimension may be filled by the separate re-embedding path. Model
revision/checksum lineage and index-generation manifests remain separate
release controls. Mutable document-row metadata cannot widen an
agent/project scope or rewrite the source identity shown in answers. The
chunk-snapshot digest covers chunk text and the ingest-time embedding vector
and is part of the version fingerprint, so changed source chunks or
ingest-time vectors cannot reuse a version identity merely by repeating a
caller-supplied source hash. Only the latest document version may be
published; a worker cannot reactivate a superseded publication by changing
the current-version pointer. Re-ingesting a historical fingerprint is a no-op
and does not roll the current pointer backward; restoring old content requires
an explicit new-version workflow and fresh governance evidence.

The database publication function requires all of the following for the exact
current document version:

1. The latest rights attestation is `cleared`, with internal storage,
   extraction, derivative, embedding and retrieval all explicitly permitted.
2. The latest technical, source-fidelity, security and QA reviews each have
   decision `passed`.
3. The latest human approval is `approved`, comes from a non-privileged
   login assigned exclusively to `rusa_knowledge_approver`, is distinct from
   the publisher, and is recorded after the latest rights attestation and all
   four passing reviews.
4. The version has an immutable chunk snapshot and a well-formed release-
   manifest URI plus lowercase SHA-256. The digest is not yet verified against
   externally stored manifest bytes.

Any new rights attestation, review finding or approval decision appended
after publication invalidates the prior publication at retrieval time. The
latest evidence must satisfy the complete gate, and a fresh publication event
is required before answers resume. Restricted rights, failed reviews and
rejected/revoked approvals therefore block retrieval immediately. Publication
and withdrawal are append-only events. A withdrawal suppresses the version
from answer-facing retrieval immediately within this PostgreSQL retrieval path.

When a cleared rights attestation requires attribution, answer-facing search,
document and published-version reads return the exact `attribution_required`
and `attribution_text` fields from the latest rights attestation. The MCP
grounded-answer prompt instructs the model to include that exact attribution
text whenever the flag is true.

Example operator flow (the login behind each command must be a member of the
role selected by `RUSA_KNOWLEDGE_DB_ROLE`):

```bash
# Rights officer / reviewer
RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_reviewer rusa-knowledge attest-rights \
  --tenant-id tenant-a --document-id <document-uuid> --version-id <version-uuid> \
  --rights-status cleared --source-owner "RUSA project owner" \
  --license-id internal-owner-authorized --internal-storage-allowed \
  --extraction-allowed --derivative-allowed --embedding-allowed \
  --retrieval-allowed --retention-policy "owner authorization" \
  --evidence-uri approval://rights/guide-v1

# Independent technical, source-fidelity, security and QA findings
RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_reviewer rusa-knowledge review-version \
  --tenant-id tenant-a --document-id <document-uuid> --version-id <version-uuid> \
  --review-type security --decision passed --finding "Security review passed"

# A different human login, assigned to the approver role
RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_approver rusa-knowledge approve-version \
  --tenant-id tenant-a --document-id <document-uuid> --version-id <version-uuid> \
  --decision approved --approval-reference ticket-001

# A separate publisher login
RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_publisher rusa-knowledge publish-version \
  --tenant-id tenant-a --document-id <document-uuid> --version-id <version-uuid> \
  --manifest-uri manifest://tenant-a/release-001 --manifest-sha256 <sha256>
```

The CLI records append-only events; it does not establish that a source's
rights claim is substantively correct. Human reviewers must inspect the
underlying terms and evidence. The release-manifest URI/hash is recorded, but
a cryptographic signature and external immutable object-store retention are
not implemented by this migration.

Withdrawal example:

```bash
RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_publisher rusa-knowledge withdraw-version \
  --tenant-id tenant-a --document-id <document-uuid> --version-id <version-uuid> \
  --reason "source owner requested withdrawal"
```

Withdrawal currently filters this PostgreSQL retrieval path. It does not yet
purge external caches, external vector indexes, source snapshots or retained
backups. A separate privileged deletion/tombstone and recovery workflow remains
a production release blocker.

## Immutable document versions

Each successful ingestion creates or reuses an immutable version fingerprint
for the document. A version records the source URI, title, content hash,
metadata, version number and ordered chunk snapshot. Re-ingesting an identical
fingerprint does not create a duplicate version. Updating a document creates a
new version while preserving earlier content and embeddings.

The current document row points to its active version through
`current_version_id`. The current retrieval chunks remain a separate
replaceable projection; version chunks are append-only snapshots. The
`rusa_knowledge_worker` role receives SELECT/INSERT only on version tables,
not UPDATE or DELETE, and is explicitly denied DELETE on current documents.
Tenant RLS applies to both version tables. Tenant erasure is intentionally not
exposed through the worker store; it requires a separate privileged retention
procedure and approved deletion workflow.

The 1536-dimensional and 384-dimensional vectors remain separate columns in
version snapshots. A 384d runtime writes `embedding_384`; a 1536d runtime
writes `embedding`. No cross-dimensional conversion is performed.

List versions and read a historical snapshot with the operator CLI:

```bash
rusa-knowledge versions --tenant-id tenant-a --collection agent \
  --document-id <document-uuid>

rusa-knowledge get-version --tenant-id tenant-a --collection agent \
  --document-id <document-uuid> --version-id <version-uuid>
```

Agent/project-specific history uses the same trusted scope filters as current
document retrieval. Supply `--agent-id` and, when applicable, `--project-id`
to the operator CLI; do not infer authorization from version metadata alone.

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
