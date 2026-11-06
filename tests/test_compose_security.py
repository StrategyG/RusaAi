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
    assert services["rusa-agent-api"]["profiles"] == ["agent"]
    assert services["rusa-agent-worker"]["profiles"] == ["agent"]
    migration_command = " ".join(services["rusa-agent-migrate"]["command"])
    assert "queue = PostgresTaskQueue(dsn)" in migration_command
    assert "queue.initialize()" in migration_command
    assert "PostgresMemoryStore(dsn).initialize()" in migration_command
    assert "queue.configure_api_permissions()" in migration_command
    worker_environment = services["rusa-agent-worker"]["environment"]
    assert worker_environment["RUSA_AGENT_PROVIDER_BASE_URL"] == "${RUSA_AGENT_PROVIDER_BASE_URL:-}"
    assert worker_environment["RUSA_AGENT_PROVIDER_API_KEY"] == "${RUSA_AGENT_PROVIDER_API_KEY:-}"
    assert worker_environment["RUSA_AGENT_MODEL"] == "${RUSA_AGENT_MODEL:-}"
    assert worker_environment["RUSA_AGENT_GITHUB_TOKEN"] == "${RUSA_AGENT_GITHUB_TOKEN:-}"


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


def test_worker_scaling_has_bounded_graceful_shutdown_and_queue_limit():
    compose = load_compose()
    worker = compose["services"]["rusa-agent-worker"]

    assert worker["restart"] == "unless-stopped"
    assert worker["stop_grace_period"] == "180s"
    assert "container_name" not in worker
    assert worker["environment"]["RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT"] == (
        "${RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT:-1000}"
    )
    assert compose["services"]["rusa-agent-api"]["environment"][
        "RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT"
    ] == "${RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT:-1000}"
    assert compose["services"]["rusa-api"]["environment"][
        "RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT"
    ] == "${RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT:-1000}"
    queue_source = (ROOT / "rusa_agents/queue.py").read_text(encoding="utf-8")
    assert "FOR UPDATE SKIP LOCKED" in queue_source
    assert "pg_advisory_xact_lock(hashtextextended(%s, 0))" in queue_source


def test_model_api_has_process_restart_and_liveness_probe():
    api = load_compose()["services"]["rusa-api"]

    assert api["restart"] == "unless-stopped"
    assert api["stop_grace_period"] == "45s"
    assert api["healthcheck"]["interval"] == "30s"
    assert "health/live" in " ".join(api["healthcheck"]["test"])


def test_encrypted_backup_service_is_opt_in_and_not_publicly_published():
    compose = load_compose()
    services = compose["services"]
    backup = services["rusa-agent-db-backup"]

    assert backup["profiles"] == ["backup-once"]
    assert backup["restart"] == "no"
    assert "ports" not in backup
    assert "rusa-agent-db-backups:/backups" in backup["volumes"]
    assert backup["environment"]["BACKUP_AGE_RECIPIENT"] == (
        "${RUSA_BACKUP_AGE_RECIPIENT:?Set RUSA_BACKUP_AGE_RECIPIENT in .env}"
    )
    assert "rusa-agent-db-backups" in compose["volumes"]
    assert (ROOT / "Dockerfile.backup").is_file()

    backup_script = (ROOT / "scripts/postgres/backup.sh").read_text(encoding="utf-8")
    restore_script = (ROOT / "scripts/postgres/restore.sh").read_text(encoding="utf-8")
    assert "pg_dump" in backup_script
    assert "age --encrypt" in backup_script
    assert "sha256sum" in backup_script
    assert "RESTORE_CONFIRM_DATABASE" in restore_script
    assert "--single-transaction" in restore_script
    assert "--clean" in restore_script

    scheduler = services["rusa-agent-db-backup-scheduler"]
    assert scheduler["profiles"] == ["backup"]
    assert scheduler["restart"] == "unless-stopped"
    assert scheduler["stop_grace_period"] == "180s"
    assert "rusa-agent-db-backups:/backups" in scheduler["volumes"]
    assert scheduler["environment"]["RUSA_BACKUP_INTERVAL_SECONDS"] == (
        "${RUSA_BACKUP_INTERVAL_SECONDS:-3600}"
    )
    assert (ROOT / "scripts/postgres/backup_scheduler.sh").is_file()


def test_inference_api_maps_queue_capacity_to_retryable_http_429():
    source = (ROOT / "llm-project/serve/app.py").read_text(encoding="utf-8")

    assert "from rusa_agents.queue import PostgresTaskQueue, QueueCapacityExceededError" in source
    assert 'headers={"Retry-After": "10"}' in source
    assert "except QueueCapacityExceededError as exc:" in source

