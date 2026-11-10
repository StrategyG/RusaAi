from rusa_agents.production_preflight import validate_production_environment


def _set_valid_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "RUSA_API_IMAGE",
        "ghcr.io/strategyg/rusa-ai-api@sha256:" + "a" * 64,
    )
    monkeypatch.setenv(
        "RUSA_WORKER_IMAGE",
        "ghcr.io/strategyg/rusa-ai-worker@sha256:" + "b" * 64,
    )
    monkeypatch.setenv(
        "RUSA_WEB_IMAGE",
        "ghcr.io/strategyg/rusa-ai-web@sha256:" + "c" * 64,
    )
    monkeypatch.setenv(
        "RUSA_SANDBOX_IMAGE",
        "ghcr.io/strategyg/rusa-ai-sandbox@sha256:" + "d" * 64,
    )
    monkeypatch.setenv(
        "RUSA_API_DATABASE_URL",
        "postgresql://rusa_api_login:db-password@db/rusa?sslmode=verify-full",
    )
    monkeypatch.setenv(
        "RUSA_WORKER_DATABASE_URL",
        "postgresql://rusa_worker_login:db-password@db/rusa?sslmode=verify-full",
    )
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        '{"' + "e" * 64 + '":{"subject":"api-test","tenant_id":"tenant-a","roles":["tenant_admin"]}}',
    )
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 48)
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "github_pat_" + "g" * 32)
    monkeypatch.setenv("RUSA_WORKSPACE_ROOT", "/srv/rusa/workspaces")
    monkeypatch.setenv("RUSA_GIT_MIRROR_ROOT", "/srv/rusa/mirrors")
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/v1")
    monkeypatch.setenv("RUSA_MODEL_API_KEY", "r" * 48)
    monkeypatch.setenv("RUSA_WEB_SESSION_SECRET", "w" * 48)


def test_production_preflight_accepts_valid_digest_pinned_configuration(monkeypatch):
    _set_valid_environment(monkeypatch)
    assert validate_production_environment() == []


def test_production_preflight_rejects_mutable_api_image_tag(monkeypatch):
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv("RUSA_API_IMAGE", "ghcr.io/strategyg/rusa-ai-api:latest")
    errors = validate_production_environment()
    assert any("RUSA_API_IMAGE" in error and "sha256" in error for error in errors)


def test_production_preflight_rejects_image_from_unapproved_registry_path(monkeypatch):
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv(
        "RUSA_WEB_IMAGE",
        "ghcr.io/untrusted/rusa-ai-web@sha256:" + "c" * 64,
    )
    errors = validate_production_environment()
    assert any("RUSA_WEB_IMAGE" in error for error in errors)
