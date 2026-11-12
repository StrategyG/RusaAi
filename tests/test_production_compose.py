def _service_block(compose: str, service: str, next_service: str) -> str:
    start = compose.index(f"  {service}:\n")
    end = compose.index(f"\n  {next_service}:\n", start)
    return compose[start:end]


def test_production_compose_uses_explicit_service_environment_allowlists():
    with open("docker-compose.production.yml", encoding="utf-8") as source:
        compose = source.read()

    assert "env_file:" not in compose

    api = _service_block(compose, "api", "web")
    web = _service_block(compose, "web", "worker")
    worker = compose.split("  worker:\n", 1)[1].split("\nnetworks:\n", 1)[0]

    assert "RUSA_API_DATABASE_URL:" in api
    assert "RUSA_INFERENCE_LEASE_SECONDS:" in api
    assert "RUSA_WORKER_INFERENCE_GATEWAY: \"false\"" in api
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
    # The worker receives only the hashed principal registry needed to bind its
    # tenant-scoped gateway credentials; raw API key material remains separate.
    assert "RUSA_API_KEYS_JSON:" in worker
    assert "RUSA_WORKER_INFERENCE_GATEWAY: \"true\"" in worker
    assert "RUSA_WORKER_INFERENCE_BASE_URL:" in worker
    assert "RUSA_WORKER_INFERENCE_API_KEYS:" in worker
    assert "RUSA_WORKER_INFERENCE_API_KEYS:" not in api
    assert "RUSA_METRICS_API_KEY:" not in worker
    assert "RUSA_API_DATABASE_URL:" not in worker
