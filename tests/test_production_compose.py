from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _service_block(compose: str, service: str, next_service: str) -> str:
    start = compose.index(f"  {service}:\n")
    end = compose.index(f"\n  {next_service}:\n", start)
    return compose[start:end]


def test_production_compose_uses_explicit_service_environment_allowlists():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")

    assert "env_file:" not in compose

    api = _service_block(compose, "api", "web")
    web = _service_block(compose, "web", "worker")
    worker = compose.split("  worker:\n", 1)[1].split("\nnetworks:\n", 1)[0]

    assert "RUSA_API_DATABASE_URL:" in api
    assert "RUSA_API_KEYS_JSON:" in api
    assert "RUSA_METRICS_API_KEY:" in api
    assert "RUSA_AUDIT_HMAC_KEY:" not in api
    assert "RUSA_GITHUB_TOKEN:" not in api
    assert "RUSA_WORKER_DATABASE_URL:" not in api

    assert "RUSA_WEB_SESSION_SECRET:" in web
    assert "RUSA_WEB_SESSION_SECRET:" not in api
    assert "RUSA_WEB_SESSION_SECRET:" not in worker

    assert "RUSA_WORKER_DATABASE_URL:" in worker
    assert "RUSA_AUDIT_HMAC_KEY:" in worker
    assert "RUSA_GITHUB_TOKEN:" in worker
    assert "RUSA_API_KEYS_JSON:" not in worker
    assert "RUSA_METRICS_API_KEY:" not in worker
    assert "RUSA_API_DATABASE_URL:" not in worker
