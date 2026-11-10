from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_production_compose_uses_explicit_service_environment_allowlists():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    api = services["api"]
    web = services["web"]
    worker = services["worker"]

    assert "env_file" not in api
    assert "env_file" not in worker

    api_env = api["environment"]
    web_env = web["environment"]
    worker_env = worker["environment"]

    assert "RUSA_API_DATABASE_URL" in api_env
    assert "RUSA_API_KEYS_JSON" in api_env
    assert "RUSA_METRICS_API_KEY" in api_env
    assert "RUSA_AUDIT_HMAC_KEY" not in api_env
    assert "RUSA_GITHUB_TOKEN" not in api_env
    assert "RUSA_WORKER_DATABASE_URL" not in api_env

    assert "RUSA_WEB_SESSION_SECRET" in web_env
    assert "RUSA_WEB_SESSION_SECRET" not in api_env
    assert "RUSA_WEB_SESSION_SECRET" not in worker_env

    assert "RUSA_WORKER_DATABASE_URL" in worker_env
    assert "RUSA_AUDIT_HMAC_KEY" in worker_env
    assert "RUSA_GITHUB_TOKEN" in worker_env
    assert "RUSA_API_KEYS_JSON" not in worker_env
    assert "RUSA_METRICS_API_KEY" not in worker_env
    assert "RUSA_API_DATABASE_URL" not in worker_env
