import pytest

from rusa_agents.config import RuntimeConfig


def test_production_config_requires_security_and_runtime_controls(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_DATABASE_URL", "postgresql://runtime:secret@db/rusa?sslmode=verify-full")
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_API_KEYS", '{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":"tenant-a"}')
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 40)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_WORKSPACE_ROOT", "/srv/rusa/workspaces")
    monkeypatch.setenv("RUSA_GIT_MIRROR_ROOT", "/srv/rusa/mirrors")
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/v1")
    monkeypatch.setenv("RUSA_SANDBOX_IMAGE", "registry.internal/sandbox@sha256:" + "a" * 64)

    config = RuntimeConfig.from_env()
    config.validate()
    assert config.tenant_repositories["tenant-a"] == ["StrategyG/RusaAi"]


def test_production_config_rejects_mutable_sandbox_tag(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_DATABASE_URL", "postgresql://db/rusa?sslmode=require")
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_API_KEYS", '{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":"tenant-a"}')
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 40)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_WORKSPACE_ROOT", "/srv/rusa/workspaces")
    monkeypatch.setenv("RUSA_GIT_MIRROR_ROOT", "/srv/rusa/mirrors")
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/v1")
    monkeypatch.setenv("RUSA_SANDBOX_IMAGE", "sandbox:latest")

    with pytest.raises(RuntimeError, match="sha256"):
        RuntimeConfig.from_env().validate()


def test_config_rejects_malformed_tenant_mapping(monkeypatch):
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", "not-json")
    with pytest.raises(ValueError, match="RUSA_TENANT_REPOSITORIES"):
        RuntimeConfig.from_env()



def test_production_config_requires_tls_for_database_and_model(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_DATABASE_URL", "postgresql://runtime:secret@db/rusa")
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_API_KEYS", '{"' + "a" * 40 + '":"tenant-a"}')
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 40)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_WORKSPACE_ROOT", "/srv/rusa/workspaces")
    monkeypatch.setenv("RUSA_GIT_MIRROR_ROOT", "/srv/rusa/mirrors")
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "http://model.internal/v1")
    monkeypatch.setenv("RUSA_SANDBOX_IMAGE", "registry.internal/sandbox@sha256:" + "a" * 64)

    with pytest.raises(RuntimeError, match="sslmode"):
        RuntimeConfig.from_env().validate()


def test_production_config_rejects_invalid_sandbox_digest(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_DATABASE_URL", "postgresql://db/rusa?sslmode=require")
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_API_KEYS", '{"' + "a" * 40 + '":"tenant-a"}')
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 40)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_WORKSPACE_ROOT", "/srv/rusa/workspaces")
    monkeypatch.setenv("RUSA_GIT_MIRROR_ROOT", "/srv/rusa/mirrors")
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/v1")
    monkeypatch.setenv("RUSA_SANDBOX_IMAGE", "registry.internal/sandbox@sha256:bad")

    with pytest.raises(RuntimeError, match="sha256"):
        RuntimeConfig.from_env().validate()


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
