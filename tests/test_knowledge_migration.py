from pathlib import Path


def test_knowledge_migration_enforces_vector_and_tenant_rls():
    migration = Path("migrations/005_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "vector(1536)" in migration
    assert "USING (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    init_script = Path("scripts/postgres/init-agent-db.sh").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "CREATE ROLE rusa_knowledge_worker" in init_script
    assert "GRANT rusa_knowledge_worker TO rusa_agent_worker" in init_script
    assert "GRANT rusa_knowledge_worker TO rusa_agent_worker" in compose
    assert "vector_cosine_ops" in migration
    assert "USING GIN (search_vector)" in migration


def test_knowledge_migration_grants_write_only_to_restricted_worker_role():
    migration = Path("migrations/005_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "rusa_knowledge_worker" in migration
    assert "rusa_agent_worker" not in migration
