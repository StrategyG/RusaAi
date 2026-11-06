from pathlib import Path


def test_rag_migration_runner_uses_database_owner_and_is_fail_fast():
    script = Path("scripts/postgres/apply-rag-migration.sh").read_text(encoding="utf-8")
    assert "set -eu" in script
    assert "RUSA_AGENT_DB_PASSWORD" in script
    assert "rusa_agent_owner" in script
    assert "ON_ERROR_STOP=1" in script
    assert "005_rag_knowledge_pgvector.sql" in script
    assert "DROP TABLE" not in script.upper()


def test_database_bootstrap_disables_implicit_rag_role_inheritance():
    script = Path("scripts/postgres/init-agent-db.sh").read_text(encoding="utf-8")
    assert "GRANT rusa_knowledge_worker TO rusa_agent_worker WITH INHERIT FALSE, SET TRUE" in script


def test_compose_migration_job_is_opt_in_and_waits_for_database():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    start = compose.index("  rusa-knowledge-migrate:")
    end = compose.index("\nvolumes:", start)
    service = compose[start:end]
    assert 'profiles: ["migrate"]' in service
    assert "rusa-agent-db:" in service
    assert "condition: service_healthy" in service
    assert "rusa_agent_owner" in service
    assert "apply-rag-migration.sh:ro" in service
    assert "005_rag_knowledge_pgvector.sql:ro" in service
