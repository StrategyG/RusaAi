# RUSA AI Free-Tier Execution Runbook

**Scope:** development and non-production pilot only.  
**Branch:** `feat/rusa-knowledge-core-phase1`  
**Services:** GitHub Codespaces, Docker pgvector (local dev), optional Supabase Free, Cloudflare R2, Hugging Face Hub, Kaggle/Colab.

This runbook separates repository changes from cloud-account actions. No third-party account, database, bucket, API key, or model artifact is provisioned by committing these files.

## 1. Start a free development environment

1. Open the repository branch in GitHub Codespaces.
2. Rebuild the Codespace using `.devcontainer/devcontainer.json`.
3. Copy `.env.free.example` to `.env.free`; keep it untracked.
4. Set a unique development-only `RUSA_DEV_DB_PASSWORD`.
5. Start the persistent local pgvector database:

```bash
docker compose --env-file .env.free -f docker-compose.free.yml up -d
docker compose --env-file .env.free -f docker-compose.free.yml ps
```

The database is mapped to loopback port 54329 and persisted in a Docker named volume. Codespaces storage remains quota-limited and can be deleted; export important data before deleting/rebuilding the Codespace. Never expose this database port publicly.

## 2. Apply RUSA migrations

Use the PostgreSQL administrator connection only for schema setup. Apply the repository's migrations in order, not only the RAG subset, because the RAG worker role depends on the existing agent runtime roles:

```bash
for migration in migrations/00{1,2,3,4,5,6,7,8,9}_*.sql migrations/010_rag_knowledge_version_history.sql; do
  test -f "$migration" || continue
  PGPASSWORD="$RUSA_DEV_DB_PASSWORD" psql \
    "host=127.0.0.1 port=54329 dbname=rusa user=postgres sslmode=disable" \
    -v ON_ERROR_STOP=1 -f "$migration"
done
```

After migrations, create a restricted login for the KnowledgeService. Use a generated password, not the example below, and execute as the database administrator:

```sql
CREATE ROLE rusa_knowledge_login LOGIN PASSWORD '<GENERATE_A_UNIQUE_SECRET>'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
    IN ROLE rusa_worker_runtime;
GRANT CONNECT ON DATABASE rusa TO rusa_knowledge_login;
```

The login is a member of the non-login worker role. The application sets `RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_worker`; PostgreSQL RLS remains the tenant boundary. Do not connect the application as postgres/superuser.

Load `.env.free` into the current shell without printing it, then set:

```bash
export RUSA_KNOWLEDGE_DATABASE_URL="postgresql://rusa_knowledge_login:<URL_ENCODED_SECRET>@127.0.0.1:54329/rusa"
export RUSA_KNOWLEDGE_DB_ROLE="rusa_knowledge_worker"
```

Install the package with the database pool extra (included in the project dependency update), then verify:

```python -m rusa_knowledge.cli check```

If migrations fail, stop. Do not bypass RLS, skip role creation, or run ingestion with the postgres superuser. Record the exact migration and error, fix compatibility in a reviewed migration, then rerun from a clean development database.

## 3. Supabase Free option — only after compatibility checks

Supabase Free may be used as a hosted non-production PostgreSQL service if its current quota and terms fit the pilot. Before using it:
1. Create a dedicated non-production project in the user's own account.
2. Enable pgvector using the provider-supported extension flow.
3. Review every migration for Supabase-managed role restrictions and ownership behavior.
4. Test migrations 001-010 on a disposable project.
5. Verify that a restricted login can assume the required RUSA roles and that `PostgresKnowledgeStore.check_ready()` passes.
6. Run the full tenant/agent/project negative RLS matrix.
7. Only then point a non-production RUSA service at its connection string.

Do not paste service-role keys or database passwords into chat, Git, issue comments, notebooks, or public Codespaces ports. Do not use Supabase's service-role key for ordinary retrieval; it bypasses RLS. Free projects may pause and have small database/storage quotas. Export before relying on the service.

## 4. RAG ingestion — strict source-pinned flow

The owner-supplied archive `RUSA_AI_RAG_Source_and_20_Agent_Packs.zip` contains the four original Markdown sources, a source/section manifest, and 20 pre-generated agent pack previews. The archive is not a database and contains no embeddings.

1. Upload/copy the ZIP into the Codespace workspace without committing it to Git.
2. Verify the archive against the repository's pinned source manifest and curated 20-agent map. This is read-only and makes no database or embedding calls:

```bash
rusa-knowledge-bundle --archive /path/to/RUSA_AI_RAG_Source_and_20_Agent_Packs.zip
```

3. Only after successful verification, extract the four original source files into the Git-ignored `docs/knowledge-seed/sources/` directory:

```bash
rusa-knowledge-bundle --archive /path/to/RUSA_AI_RAG_Source_and_20_Agent_Packs.zip --extract
```

The verifier checks all four source byte counts and SHA-256 hashes, all 20 profile IDs/headings against the repository map, every raw extracted-section SHA-256, each pack's provenance frontmatter, ZIP path/symlink/duplicate-member safety, and bounded archive sizes. It also reports the normalized section hash used by the current miner. The pre-generated pack preview's raw section hash and the current miner's normalized content hash are intentionally reported separately; do not treat them as the same digest.

4. Run the four-document no-write preflight:

```bash
python -m rusa_knowledge.seed
```

5. Review the report and credential scan. Expected source corpus is four documents and 81 chunks for the pinned source versions. A mismatch blocks ingestion.
6. Run the 20-profile mining preflight using the exact source map and source documents:

```bash
python -m rusa_knowledge.agent_mining
```

The pinned map currently yields 20 agent documents and 60 chunks with the current 1200/180 chunker. The miner recomputes normalized section hashes and preserves source line locators; it does not ingest the pre-generated pack preview files directly.

7. Review source statuses: architecture/catalog are unspecified, ADR and lifecycle are proposed; all are currently UNVERIFIED and NOT_VERIFIED for implementation. Do not relabel them as implemented.
8. Only after source review, database readiness, embedding-cost approval, and tenant authorization, apply the shared source corpus:

```bash
python -m rusa_knowledge.seed --tenant-id '<authorized-rusa-tenant>' --apply
```

9. Apply the 20 agent-specific documents as a separate, auditable operation:

```bash
python -m rusa_knowledge.agent_mining --tenant-id '<authorized-rusa-tenant>' --apply
```

10. Preserve ingestion output, hashes, counts, and database readiness report as evidence.
11. Run RAG evaluation with a human-reviewed gold set. Do not claim quality based only on successful ingestion.

No embeddings or database rows are created by source preflight. The 20 agent packs are curated extractions from the supplied source documents, not 20 independently verified implementations.

## 5. Embedding cost and dimension constraint

The current RUSA schema fixes pgvector at 1536 dimensions. `KnowledgeService` defaults to the OpenAI-compatible `text-embedding-3-small` endpoint and expects 1536-dimensional vectors. The API key/service is not free by virtue of using free database storage.

A replacement free/local embedding model must return exactly 1536 dimensions and pass retrieval evaluation. Common free embedding models often emit 384, 768, or 1024 dimensions; they cannot be plugged into this schema unchanged. Do not truncate, pad, or silently mix embedding spaces. A model/dimension change requires a versioned migration, re-embedding all active and historical corpus as appropriate, and a new frozen retrieval benchmark.

## 6. Free GPU training and artifact handling

- Use Kaggle or Colab only for bounded, interactive training/evaluation jobs subject to their current quota and acceptable-use policies.
- Do not run a persistent inference API or privileged MCP endpoint inside a notebook session.
- Use the pinned base model revision and training config. Run a small smoke test first.
- Keep baseline train/eval and the 48-record synthetic candidate separate. The candidate requires human review; do not mix it into the held-out evaluation set.
- Save adapter/checkpoint outputs to a durable authorized private artifact store before the notebook expires.
- Verify SHA-256 after export. Store model manifest, dataset hashes, base revision, training commit, hyperparameters, seed, and evaluation report alongside the artifact.
- Never upload secrets, personal data, confidential source code, or unapproved production traces to public Hugging Face repositories or notebooks.
- Training completion is not model promotion. Require held-out task, security, blocked-action, and regression acceptance tests.

## 7. Cloudflare R2 and Hugging Face Hub

R2 is an object store for source archives and approved artifacts; it does not replace PostgreSQL/pgvector. Create a private bucket in the user's account, issue a narrowly scoped bucket token, and keep it in a secret manager. Start within the free allowance and monitor usage.

Use Hugging Face Hub for model/dataset version distribution only when visibility, license, and data rights are approved. Public repositories are public. Private free storage quotas and policies can change; do not treat either R2 or Hub as sole institutional backup. Keep encrypted independent backups of manifests and source originals.

## 8. Required acceptance checks before pilot sign-off

- [ ] Clean Codespace rebuild succeeds without manual undocumented state.
- [ ] Dev database starts and persists across stop/start (without volume deletion).
- [ ] Migrations 001-010 apply in order on a clean PostgreSQL 16 + pgvector database.
- [ ] KnowledgeService connects as restricted login, never postgres/superuser.
- [ ] `rusa-knowledge check` reports ready.
- [ ] Four source hashes match the manifest; no credential scan finding.
- [ ] 20 agent profiles pass exact heading extraction and provenance validation.
- [ ] Ingestion is idempotent and uses the explicitly authorized tenant.
- [ ] Cross-tenant/agent/project negative tests deny all unauthorized reads.
- [ ] Embedding vectors are 1536-dimensional and retrieval benchmark results are recorded.
- [ ] Dataset schemas and provenance validate; candidate examples remain unpromoted until reviewed.
- [ ] GPU smoke test exports a checksummed adapter before session expiry.
- [ ] MCP is not hosted from a notebook; tool access remains scoped and audited.
- [ ] Autonomous changes run in an isolated workspace/branch with independent QA.
- [ ] Backup/export and restore have been exercised.

## 9. Operational boundary

Free tiers are suitable for engineering and a small non-production pilot, not a 24/7 institutional production promise. Before production, migrate any component whose quota, availability, privacy, recovery, support, or compliance requirements exceed its free plan. Keep provider interfaces and export formats portable.
