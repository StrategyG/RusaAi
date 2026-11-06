import pytest

from rusa_agents.config import RuntimeConfig


def set_api_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "RUSA_API_DATABASE_URL",
        "postgresql://rusa_api_login:secret@db/rusa?sslmode=verify-full",
    )
    monkeypatch.setenv("RUSA_API_KEYS", '{"' + "a" * 40 + '":"tenant-a"}')
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 40)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/v1")


def set_worker_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "RUSA_WORKER_DATABASE_URL",
        "postgresql://rusa_worker_login:secret@db/rusa?sslmode=verify-full",
    )
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_WORKSPACE_ROOT", "/srv/rusa/workspaces")
    monkeypatch.setenv("RUSA_GIT_MIRROR_ROOT", "/srv/rusa/mirrors")
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "github_pat_" + "g" * 32)
    monkeypatch.setenv(
        "RUSA_SANDBOX_IMAGE",
        "registry.internal/sandbox@sha256:" + "a" * 64,
    )
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/v1")


def test_production_api_accepts_dedicated_tls_database_role(monkeypatch):
    set_api_environment(monkeypatch)
    RuntimeConfig.from_env().validate_api()


def test_production_worker_accepts_dedicated_tls_database_role(monkeypatch):
    set_worker_environment(monkeypatch)
    RuntimeConfig.from_env().validate_worker()


def test_production_api_rejects_database_without_tls(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.setenv(
        "RUSA_API_DATABASE_URL",
        "postgresql://rusa_api_login:secret@db/rusa",
    )
    with pytest.raises(RuntimeError, match="sslmode"):
        RuntimeConfig.from_env().validate_api()


def test_production_api_rejects_legacy_shared_database_dsn(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.delenv("RUSA_API_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "RUSA_DATABASE_URL",
        "postgresql://rusa:secret@db/rusa?sslmode=verify-full",
    )
    with pytest.raises(RuntimeError, match="RUSA_API_DATABASE_URL"):
        RuntimeConfig.from_env().validate_api()


def test_production_worker_rejects_api_database_identity(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv(
        "RUSA_WORKER_DATABASE_URL",
        "postgresql://rusa_api_login:secret@db/rusa?sslmode=verify-full",
    )
    with pytest.raises(RuntimeError, match="rusa_worker_login"):
        RuntimeConfig.from_env().validate_worker()


def test_production_worker_rejects_mutable_sandbox_tag(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv("RUSA_SANDBOX_IMAGE", "sandbox:latest")
    with pytest.raises(RuntimeError, match="sha256"):
        RuntimeConfig.from_env().validate_worker()


def test_production_worker_rejects_public_placeholder_secrets(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "AT_LEAST_32_RANDOM_UTF8_BYTES")
    with pytest.raises(RuntimeError, match="placeholder|real secret"):
        RuntimeConfig.from_env().validate_worker()


def test_config_rejects_malformed_tenant_mapping(monkeypatch):
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", "not-json")
    with pytest.raises(ValueError, match="RUSA_TENANT_REPOSITORIES"):
        RuntimeConfig.from_env()


def test_aicode_kill_switch_defaults_to_disabled_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("RUSA_AICODE_ENABLED", raising=False)
    assert RuntimeConfig.from_env().aicode_enabled is False


def test_aicode_kill_switch_defaults_to_enabled_in_development(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_AICODE_ENABLED", raising=False)
    assert RuntimeConfig.from_env().aicode_enabled is True


def test_aicode_kill_switch_rejects_ambiguous_values(monkeypatch):
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="RUSA_AICODE_ENABLED"):
        RuntimeConfig.from_env()



def test_production_api_rejects_database_url_without_host(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.setenv(
        "RUSA_API_DATABASE_URL",
        "postgresql://rusa_api_login:secret@/rusa?sslmode=verify-full",
    )
    with pytest.raises(RuntimeError, match="database host"):
        RuntimeConfig.from_env().validate_api()


def test_production_api_rejects_unmapped_api_key_tenant(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-b":["StrategyG/RusaAi"]}')
    with pytest.raises(RuntimeError, match="repository allowlist"):
        RuntimeConfig.from_env().validate_api()
