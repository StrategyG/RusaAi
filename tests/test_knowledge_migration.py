import re
from pathlib import Path


def test_knowledge_migration_enforces_vector_and_tenant_rls():
    migration = Path("migrations/005_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "vector(1536)" in migration
    assert "USING (tenant_id = current_setting('rusa.tenant_id', true))" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "CREATE ROLE rusa_knowledge_worker" in migration
    assert "GRANT rusa_knowledge_worker TO rusa_agent_worker WITH INHERIT FALSE, SET TRUE" in migration
    assert "vector_cosine_ops" in migration
    assert "USING GIN (search_vector)" in migration


def test_knowledge_migration_grants_write_only_to_restricted_worker_role():
    migration = Path("migrations/005_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in migration
    assert "ON public.rusa_knowledge_documents TO rusa_knowledge_worker" in migration
    assert "ON public.rusa_knowledge_chunks TO rusa_knowledge_worker" in migration
    assert not re.search(
        r"GRANT\s+[^;]+\s+ON\s+public\.rusa_knowledge_(?:documents|chunks)\s+TO\s+rusa_agent_worker",
        migration,
        flags=re.IGNORECASE,
    )

def test_fresh_database_bootstrap_requires_explicit_rag_role_switch():
    bootstrap = Path("scripts/postgres/init-agent-db.sh").read_text(encoding="utf-8")
    assert "GRANT rusa_knowledge_worker TO rusa_agent_worker WITH INHERIT FALSE, SET TRUE" in bootstrap
\n