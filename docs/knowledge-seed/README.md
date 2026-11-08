# RUSA AI Knowledge Seed — Architecture & Agents

This seed manifest prepares the first centralized RUSA AI knowledge corpus from four source documents supplied by the project owner. The repository is public, so the original source documents are intentionally **not committed**. Place the exact originals in the ignored `sources/` directory before running ingestion.

## Scope

| Source ID | File | Domain | Source status |
|---|---|---|---|
| RUSA-ARCH-001 | `RUSA_AI_Architecture.md` | Architecture | Unspecified in source |
| RUSA-AGENT-001 | `RUSA_AI_All_Agents-2.md` | Agent catalog | Unspecified in source |
| RUSA-COMM-001 | `ADR-RUSA-AI-Komunikasi-Agent(1).md` | Agent communication | Diusulkan |
| RUSA-DEVOPS-001 | `RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE(1).md` | Agentic DevOps | Proposed Architecture |

The manifest pins the SHA-256 digest of each original file. Do not edit, normalize, or rename the source files. A mismatch stops ingestion before any embedding or database write.

All four documents are marked `UNVERIFIED` and `NOT_VERIFIED` for implementation status. The ADR and DevOps lifecycle retain their proposed design status. This is deliberate: a design document is not proof that a feature is deployed.

The agent names in separate documents are preserved independently. In particular, this seed does not automatically merge similarly named or potentially overlapping agent definitions.

## 1. Place source documents locally

Copy the four original uploaded Markdown files into:

```text
docs/knowledge-seed/sources/
```

This directory is ignored by Git to avoid publishing internal source material.

## 2. Prepare the knowledge database

The existing RUSA RAG implementation uses PostgreSQL 16 with pgvector and 1536-dimensional embeddings. It retains the existing vector store and adds `migrations/008_rag_agent_scope_indexes.sql` for the shared/agent-specific metadata filters; this seed does not introduce Qdrant or a second vector store.

Apply migrations `007_rag_knowledge_pgvector.sql`, `008_rag_agent_scope_indexes.sql`, and `009_rag_project_scope_index.sql`, in that order, using a database owner/administrator connection to the dedicated knowledge database. Then configure a restricted non-superuser login that can assume the `rusa_knowledge_worker` role.

Set these environment variables outside the repository, using a secret manager where appropriate:

```bash
export RUSA_KNOWLEDGE_DATABASE_URL='postgresql://<restricted-login>:<password>@<host>/<knowledge-db>'
export RUSA_KNOWLEDGE_DB_ROLE='rusa_knowledge_worker'
export RUSA_EMBEDDING_BASE_URL='https://api.openai.com/v1'
export RUSA_EMBEDDING_API_KEY='<secret>'
export RUSA_EMBEDDING_MODEL='text-embedding-3-small'
```

The embedding provider must return 1536-dimensional vectors. Never commit database URLs, passwords, API keys, bearer tokens, or raw tenant credentials.

## 3. Run a non-writing preflight

From the repository root:

```bash
python -m rusa_knowledge.seed
```

The default source directory is `docs/knowledge-seed/sources`. Override it with `--source-dir` if the originals are stored elsewhere.

Preflight validates:
- all four source files exist and are regular files;
- no source path is a symlink or escapes the source directory;
- each file matches its pinned SHA-256 digest;
- document parsing succeeds;
- credential-material checks pass;
- metadata conforms to the existing RAG metadata limits;
- the current chunker can produce searchable chunks.

Preflight does not connect to PostgreSQL, call the embedding provider, or write data.

With the exact source files and the current 1200-character chunk / 180-character overlap defaults, the expected seed is **4 documents and 81 chunks**:
- Architecture: 1 chunk
- All Agents: 30 chunks
- Communication ADR: 22 chunks
- Agentic DevOps Lifecycle: 28 chunks

These are preflight expectations, not a claim that the database already contains the records.

## 4. Ingest into the authorized tenant

Choose the actual RUSA tenant that is authorized to hold shared internal platform knowledge. The tenant must already be valid in the deployment's identity and access model; this script does not create tenants or grant access.

After reviewing a successful preflight, explicitly apply:

```bash
python -m rusa_knowledge.seed --tenant-id '<authorized-rusa-tenant>' --apply
```

The default collection is `agent`, matching the existing worker and MCP retrieval defaults. The four initial architecture/agent source documents are marked `knowledge_scope=shared`, so all authorized agent workers may retrieve them. Future agent-specific documents must set `knowledge_scope=agent_specific` and provide one or more validated `agent_ids`. If `agent_ids` is present, `knowledge_scope` must be explicit; the system will not infer shared visibility. The worker gateway supplies its AICode scope ID (for example, `aicode-analyst`) from the trusted WorkerRole and derives a project scope from the already-authorized repository target (for example, `strategyg--rusaai`). Agent-specific knowledge without a project ID is agent-wide within the tenant; records with a project ID are visible only to that agent in the matching project. Models cannot choose the scope, project, or collection.

The source URI is stable per source ID, so re-ingesting the same source updates the same document row rather than creating a duplicate row. Re-ingestion still recomputes embeddings and replaces that document's chunks.

The script prints document IDs, chunk counts, and content hashes. Keep this output as the ingestion record; do not log secrets or full source bodies.

## 5. Verify after ingestion

Run:

```bash
rusa-knowledge check
```

Then verify the `agent` collection for the selected tenant using the existing read-only MCP tools or a tenant-authorized database query. Confirm 4 documents and 81 chunks before enabling worker retrieval.

The MCP service is separately deployed and read-only. The core API does not enable RAG merely because the package is installed. Worker retrieval remains disabled unless `RUSA_RAG_ENABLED=true` is explicitly configured after database roles, migration, and readiness checks are complete.

## Operational boundaries

- This seed does not deploy PostgreSQL, the MCP service, or worker retrieval.
- Current retrieval uses pgvector plus PostgreSQL full-text search with weighted reciprocal-rank fusion; no external reranker is added here.
- The feature branch applies the same metadata visibility filter to vector and lexical candidates before fusion: shared records plus records tagged for the authenticated worker scope.
- The RUSA Knowledge MCP endpoint has tenant identity but no authenticated agent identity mapping in this increment, so it can retrieve shared records only.
- Retrieved content is evidence, not instructions.
- Proposed design records remain visibly proposed and unverified.
- Tenant isolation and forced row-level security remain mandatory.
- The current database schema stores the current document row per stable source URI; it does not retain immutable historical document versions. Source-file history remains in the controlled source repository until a dedicated version-history migration is designed.
