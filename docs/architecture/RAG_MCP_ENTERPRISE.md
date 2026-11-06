# RUSA-AI RAG and MCP Enterprise Architecture

## Status and scope

This implementation adds a tenant-scoped retrieval service and a standards-based MCP server/client. It is a deployable code foundation, not a production certification. Production activation requires the pgvector migration, database roles, embedding-provider credentials, TLS ingress, tenant tokens, retrieval evaluation, and staging verification.

## Architecture

~~~mermaid
flowchart TB
    U[Operator / Document Owner] --> ING[Local Ingestion CLI]
    ING --> PAR[TXT / MD / HTML / PDF / DOCX Parser]
    PAR --> CH[Normalization and Overlap Chunking]
    CH --> EMB[OpenAI-Compatible Embedding Provider]
    EMB --> PG[(PostgreSQL + pgvector)]
    PG --> V[Vector Search]
    PG --> F[PostgreSQL Full-Text Search]
    V --> RRF[Hybrid Reciprocal Rank Fusion]
    F --> RRF
    RRF --> KS[Knowledge Service]
    KS --> MCP[MCP Server / Streamable HTTP]
    HOST[MCP Host / Agent Worker] <--> CLIENT[MCP Client]
    CLIENT <--> MCP
    MCP --> AUTH[Bearer Hash Authentication]
    AUTH --> TEN[Tenant Context + PostgreSQL RLS]
    TEN --> PG
    WORKER[Hierarchical Agent Workers] --> MEM[Composite Episodic + RAG Memory]
    MEM --> KS
    WORKER --> CLIENT
    WORKER --> MASTER[Master Orchestrator]
~~~

## RAG pipeline

1. An operator ingests a local file. Remote URLs are not fetched, preventing ingestion-time SSRF.
2. TXT, Markdown, HTML, PDF, and DOCX parsers extract bounded text. The default source size is 10 MiB and extracted text is capped at 2 million characters.
3. Text is normalized and divided into 1,200-character chunks with 180-character overlap. Chunking prefers paragraph and sentence boundaries.
4. An OpenAI-compatible embeddings endpoint returns 1,536-dimensional vectors. The client validates response shape, vector indexes, dimensions, finite values, and non-zero vectors.
5. The store writes document metadata and chunks in one PostgreSQL transaction.
6. Retrieval runs vector cosine search and PostgreSQL full-text search in parallel. Results are combined using weighted reciprocal-rank fusion (70% vector rank, 30% lexical rank).
7. Every result carries document ID, source URI, title, chunk index, and retrieval scores for source-grounded generation.

The current migration fixes dimensions at 1,536 to match text-embedding-3-small. A different embedding model or dimension requires a reviewed schema migration and re-indexing; do not change only the environment variable.

## MCP surface

Transport: MCP Streamable HTTP using the official MCP Python SDK v2.

Read-only tools:
- search_knowledge(query, collection, limit)
- get_document(document_id, collection)
- list_collections()

Resource:
- knowledge://capabilities

Prompt:
- answer_with_sources(question)

No document-write, arbitrary HTTP, shell, SQL, filesystem, or administrative tools are exposed over MCP. Ingestion is a separate operator CLI workflow.

## Tenant isolation and security

- MCP requests require an Authorization Bearer token. The server stores only SHA-256 token digests in RUSA_MCP_TENANT_KEYS, mapped to tenant IDs.
- The client never supplies tenant_id as a tool argument. The server derives tenant identity from the authenticated token and a request-local ContextVar.
- Every database operation sets rusa.tenant_id transaction-locally. Both knowledge tables enable and FORCE PostgreSQL RLS.
- The MCP service uses the restricted rusa_agent_api role with SELECT-only grants on knowledge tables. The worker uses its isolated worker role for ingestion and internal RAG retrieval.
- The agent MCP gateway has a fixed read-only tool allowlist and a separate token per tenant.
- MCP endpoint URLs must use HTTPS, except loopback HTTP for local development or an explicitly configured private service hostname in RUSA_MCP_INTERNAL_HTTP_HOSTS. Use that exception only on a private container network; do not expose the plain-HTTP service port to untrusted networks. Redirects and ambient proxy inheritance are disabled in the client.
- Retrieved content is untrusted data. It must never override system policy or be interpreted as executable instructions.
- Health liveness is public. Readiness reports only a generic status and does not expose connection strings, credentials, or tenant data.

Generate a tenant token with a password manager or secrets.token_urlsafe(48). Configure the plaintext token only in the worker/secret manager. Configure its SHA-256 digest in the MCP server's tenant-key map. Never commit either value.

## Database deployment

Use a PostgreSQL build with pgvector installed, for example pgvector/pgvector:pg16. Apply migrations/005_rag_knowledge_pgvector.sql with a database migration identity that can install the vector extension and create the schema. The migration:
- creates the document and chunk tables;
- creates vector HNSW and full-text GIN indexes;
- enables and forces tenant RLS;
- creates transaction-local tenant policies;
- grants read-only access to rusa_agent_api and read/write access to rusa_agent_worker when those roles exist.

Do not run the migration as the MCP application identity. Existing database volumes do not replay initialization scripts automatically. Apply and verify the migration explicitly.

## Configuration

Required when enabling RAG or MCP:
- RUSA_KNOWLEDGE_DATABASE_URL: database DSN for the service.
- RUSA_EMBEDDING_BASE_URL: HTTPS or loopback OpenAI-compatible embedding endpoint.
- RUSA_EMBEDDING_MODEL: embedding model returning 1,536 dimensions.
- RUSA_EMBEDDING_API_KEY: provider key, if required by the provider.
- RUSA_MCP_TENANT_KEYS: JSON object mapping SHA-256 bearer-token digests to tenant IDs.
- RUSA_MCP_ALLOWED_HOSTS: comma-separated Host header allowlist for the deployed MCP endpoint.

Optional agent-worker integration:
- RUSA_RAG_ENABLED=true adds semantic retrieval to the existing episodic memory reader.
- RUSA_KNOWLEDGE_COLLECTION=agent selects the worker knowledge collection.
- RUSA_MCP_SERVER_URL enables the remote MCP tool gateway.
- RUSA_MCP_TENANT_TOKENS maps tenant IDs to their secret bearer tokens.
- RUSA_MCP_ALLOWED_TOOLS defaults to the three read-only knowledge tools.

## Local ingestion

Install requirements-rag-mcp.txt in a Python 3.11+ environment, apply the migration, and set the database and embedding variables. Then run:

~~~bash
python -m rusa_knowledge.cli check
python -m rusa_knowledge.cli ingest --file ./docs/guide.pdf --tenant-id tenant-a --collection agent
~~~

The source URI defaults to the resolved local file URI. For a portable citation, provide an explicit stable source URI with --source-uri.

Prepare a JSONL relevance dataset with one object per line, for example:

~~~json
{"query":"How is tenant isolation enforced?","relevant_document_ids":["00000000-0000-0000-0000-000000000001"]}
~~~

Run retrieval evaluation with:

~~~bash
python -m rusa_knowledge.cli evaluate --dataset ./eval.jsonl --tenant-id tenant-a --collection agent --k 5
~~~

The report includes Recall@K, MRR@K, and nDCG@K. The evaluation CLI deduplicates repeated chunk hits by document ID. Keep gold datasets tenant-separated and review relevance labels before using them as release gates.

## Local MCP service

Run the service with a tenant token hash configured:

~~~bash
uvicorn rusa_mcp.server:app --host 127.0.0.1 --port 8002
~~~

Connect an MCP Inspector or host to http://127.0.0.1:8002/mcp and supply the bearer token. For remote deployments, use HTTPS and set RUSA_MCP_ALLOWED_HOSTS to the exact hostnames accepted by the reverse proxy.

The Docker Compose service is under the knowledge profile. Start it only after applying the database migration and configuring the restricted database URL, embedding provider, host allowlist, and tenant key hashes.

## Evaluation and release gates

Do not certify RAG quality from unit tests alone. Before production, build a tenant-separated gold dataset and measure:
- Recall@5 and Recall@10;
- MRR@10 and nDCG@10;
- citation/source accuracy;
- answer faithfulness and unsupported-claim rate;
- p50/p95 retrieval latency and embedding latency;
- tenant-isolation and deletion tests;
- prompt-injection resistance for hostile document content.

Recommended initial release gates: Recall@5 >= 0.85, source attribution accuracy >= 0.98, zero cross-tenant results in adversarial tests, and no critical/high security findings. Thresholds must be calibrated against the actual domain corpus and approved by the owner.

Operational gates:
- verify PostgreSQL backups and restore;
- set connection-pool and ingress limits;
- monitor embedding/provider errors, retrieval latency, pool saturation, and MCP tool failures;
- set document retention and tenant deletion policy;
- pin container images and dependencies and generate an SBOM;
- complete staging soak, security review, and rollback rehearsal.

A green CI run proves only the checks executed on that commit. It does not prove retrieval relevance, provider availability, production capacity, or security certification.
