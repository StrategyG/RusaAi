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
