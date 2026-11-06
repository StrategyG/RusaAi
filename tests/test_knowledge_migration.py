from pathlib import Path


def test_knowledge_migration_enforces_vector_and_tenant_rls():
    migration = Path("migrations/005_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "vector(1536)" in migration
    assert "USING (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "vector_cosine_ops" in migration
    assert "USING GIN (search_vector)" in migration
