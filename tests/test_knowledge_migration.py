from pathlib import Path


def test_knowledge_migration_enforces_vector_and_tenant_rls():
    migration = Path("migrations/007_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "vector(1536)" in migration
    assert "USING (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "rusa_knowledge_worker" in migration


def test_384d_migration_preserves_legacy_vectors_and_adds_parallel_index():
    migration = Path("migrations/009_knowledge_embedding_384.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS embedding_384 vector(384)" in migration
    assert "ALTER COLUMN embedding DROP NOT NULL" in migration
    assert "embedding IS NOT NULL OR embedding_384 IS NOT NULL" in migration
    assert "rusa_knowledge_chunks_embedding_384_hnsw_idx" in migration
    assert "DROP COLUMN" not in migration
    assert "UPDATE public.rusa_knowledge_chunks" not in migration


def test_agent_project_scope_migration_adds_lookup_indexes():
    migration = Path("migrations/011_rag_agent_project_scope.sql").read_text(encoding="utf-8")
    assert "rusa_knowledge_documents_scope_idx" in migration
    assert "rusa_knowledge_documents_agent_ids_gin" in migration
    assert "rusa_knowledge_documents_project_scope_idx" in migration
    assert "tenant_id" in migration
    assert "DROP " not in migration


def test_document_version_history_migration_is_append_only_and_dimension_aware():
    migration = Path("migrations/012_rag_knowledge_version_history.sql").read_text(
        encoding="utf-8"
    )
    assert "rusa_knowledge_document_versions" in migration
    assert "rusa_knowledge_version_chunks" in migration
    assert "version_fingerprint" in migration
    assert "version_number" in migration
    assert "embedding vector(1536)" in migration
    assert "embedding_384 vector(384)" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "GRANT SELECT, INSERT" in migration
    assert "GRANT UPDATE" not in migration
    assert "GRANT DELETE" not in migration
    assert "current_version_id" in migration
