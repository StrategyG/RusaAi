import pytest

from rusa_agents.config import RuntimeConfig


def test_production_config_requires_security_and_runtime_controls(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_DATABASE_URL", "postgresql://runtime:secret@db/rusa")
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_API_KEYS", '{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":"tenant-a"}')
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
    monkeypatch.setenv("RUSA_DATABASE_URL", "postgresql://db/rusa")
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_API_KEYS", '{"api-secret":"tenant-a"}')
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
