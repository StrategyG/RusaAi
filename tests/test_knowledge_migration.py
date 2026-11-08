from pathlib import Path


def test_knowledge_migration_enforces_vector_and_tenant_rls():
    migration = Path("migrations/007_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "vector(1536)" in migration
    assert "USING (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "rusa_knowledge_worker" in migration


def test_agent_scope_migration_adds_metadata_indexes():
    migration = Path("migrations/008_rag_agent_scope_indexes.sql").read_text(encoding="utf-8")
    assert "rusa_knowledge_documents_scope_idx" in migration
    assert "rusa_knowledge_documents_agent_ids_gin" in migration
    assert "metadata->'agent_ids'" in migration
    assert "BEGIN;" in migration
    assert "COMMIT;" in migration


def test_project_scope_migration_adds_partial_lookup_index():
    migration = Path("migrations/009_rag_project_scope_index.sql").read_text(encoding="utf-8")
    assert "rusa_knowledge_documents_project_scope_idx" in migration
    assert "metadata->>'project_id'" in migration
    assert "knowledge_scope' = 'agent_specific'" in migration
    assert "BEGIN;" in migration
    assert "COMMIT;" in migration
