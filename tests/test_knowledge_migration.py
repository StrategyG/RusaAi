from pathlib import Path


def test_knowledge_migration_enforces_vector_and_tenant_rls():
    migration = Path("migrations/007_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "vector(1536)" in migration
    assert "USING (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "rusa_knowledge_worker" in migration


def test_embedding_profile_migration_prevents_mixed_models():
    migration = Path("migrations/008_rag_embedding_profiles.sql").read_text(encoding="utf-8")
    assert "rusa_knowledge_collection_profiles" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "rusa_knowledge_profiles_tenant_isolation" in migration
    assert "rusa_knowledge_worker" in migration
