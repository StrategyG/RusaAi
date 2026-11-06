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
