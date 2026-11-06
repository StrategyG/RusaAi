from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_compose():
    with (ROOT / "docker-compose.yml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_api_host_ports_are_loopback_only():
    services = load_compose()["services"]

    assert services["rusa-api"]["ports"] == [
        "${RUSA_BIND_ADDRESS:-127.0.0.1}:${RUSA_PORT:-8000}:8000"
    ]
    assert services["rusa-agent-api"]["ports"] == [
        "127.0.0.1:${RUSA_AGENT_PORT:-8001}:8001"
    ]
    assert "ports" not in services["rusa-agent-db"]
    assert "ports" not in services["rusa-agent-worker"]
    assert "ports" not in services["rusa-agent-migrate"]


def test_schema_and_privilege_migration_gates_api_and_worker():
    services = load_compose()["services"]

    assert services["rusa-agent-migrate"]["depends_on"] == {
        "rusa-agent-db": {"condition": "service_healthy"}
    }
    assert services["rusa-agent-api"]["depends_on"] == {
        "rusa-agent-migrate": {"condition": "service_completed_successfully"}
    }
    assert services["rusa-agent-worker"]["depends_on"] == {
        "rusa-agent-migrate": {"condition": "service_completed_successfully"}
    }
    assert services["rusa-agent-migrate"]["restart"] == "no"
    migration_command = " ".join(services["rusa-agent-migrate"]["command"])
    assert "queue = PostgresTaskQueue(dsn)" in migration_command
    assert "queue.initialize()" in migration_command
    assert "PostgresMemoryStore(dsn).initialize()" in migration_command
    assert "queue.configure_api_permissions()" in migration_command


def test_knowledge_service_is_optional_and_loopback_bound():
    services = load_compose()["services"]

    assert services["rusa-agent-db"]["image"] == "pgvector/pgvector:pg16"
    assert services["rusa-mcp"]["profiles"] == ["knowledge"]
    assert services["rusa-mcp"]["ports"] == ["127.0.0.1:${RUSA_MCP_PORT:-8002}:8002"]
    assert services["rusa-mcp"]["depends_on"] == {
        "rusa-agent-migrate": {"condition": "service_completed_successfully"}
    }
    assert "ports" not in services["rusa-agent-worker"]
    migration_command = " ".join(services["rusa-agent-migrate"]["command"])
    assert "005_rag_knowledge_pgvector.sql" in migration_command


def test_user_admin_workspace_is_loopback_published_and_migration_gated():
    services = load_compose()["services"]

    assert services["rusa-web"]["ports"] == ["127.0.0.1:${RUSA_WEB_PORT:-3000}:8080"]
    assert services["rusa-web"]["depends_on"] == {
        "rusa-api": {"condition": "service_healthy"}
    }
    assert services["rusa-api"]["depends_on"] == {
        "rusa-agent-migrate": {"condition": "service_completed_successfully"}
    }
    assert "rusa-agent-audit-data:/var/lib/rusa-agent" in services["rusa-api"]["volumes"]
    assert "RUSA_AUTH_SCHEMA_MANAGED_EXTERNALLY" in services["rusa-api"]["environment"]
    assert "006_user_identity.sql" in " ".join(services["rusa-agent-migrate"]["command"])
    assert services["rusa-agent-db"]["environment"]["RUSA_WEB_DB_PASSWORD"] == "${RUSA_WEB_DB_PASSWORD:?Set RUSA_WEB_DB_PASSWORD in .env}"
    assert services["rusa-agent-migrate"]["environment"]["RUSA_WEB_DB_PASSWORD"] == "${RUSA_WEB_DB_PASSWORD:?Set RUSA_WEB_DB_PASSWORD in .env}"

    init_script = (ROOT / "scripts/postgres/init-agent-db.sh").read_text(encoding="utf-8")
    assert "RUSA_WEB_DB_PASSWORD is required" in init_script
    assert 'case "$RUSA_WEB_DB_PASSWORD" in' in init_script
