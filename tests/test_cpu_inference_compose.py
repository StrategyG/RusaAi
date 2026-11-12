from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_base_compose_does_not_share_api_model_secret_with_worker():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    worker_environment = compose["services"]["worker"]["environment"]

    assert worker_environment["RUSA_MODEL_API_KEY"] == "${RUSA_WORKER_MODEL_API_KEY:-}"


def test_production_worker_uses_tenant_scoped_https_api_gateway():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    worker = compose["services"]["worker"]
    worker_environment = worker["environment"]
    api = compose["services"]["api"]
    api_environment = api["environment"]

    assert worker_environment["RUSA_MODEL_API_KEY"] == ""
    assert api_environment["RUSA_WORKER_INFERENCE_API_KEYS"] == ""
    assert api_environment["RUSA_WORKER_INFERENCE_GATEWAY"] == "false"
    assert api_environment["SSL_CERT_FILE"] == "/run/secrets/rusa-internal-ca.crt"
    assert any(
        "/run/secrets/rusa-internal-ca.crt:ro" in volume
        for volume in api["volumes"]
    )
    assert worker["depends_on"]["api"]["condition"] == "service_healthy"
    assert worker_environment["RUSA_WORKER_INFERENCE_GATEWAY"] == "true"
    assert worker_environment["RUSA_WORKER_INFERENCE_BASE_URL"].startswith(
        "${RUSA_WORKER_INFERENCE_BASE_URL:?"
    )
    assert "HTTPS" in worker_environment["RUSA_WORKER_INFERENCE_BASE_URL"]
    assert "RUSA_WORKER_INFERENCE_API_KEYS" in worker_environment
    assert "RUSA_API_KEYS_JSON" in worker_environment
    assert worker_environment["SSL_CERT_FILE"] == "/run/secrets/rusa-internal-ca.crt"
    assert any(
        "/run/secrets/rusa-internal-ca.crt:ro" in volume
        for volume in compose["services"]["worker"]["volumes"]
    )
    assert compose["services"]["worker"]["depends_on"]["api"]["condition"] == (
        "service_healthy"
    )


def test_cpu_compose_routes_api_to_private_gguf_server():
    compose = (ROOT / "docker-compose.cpu.yml").read_text(encoding="utf-8")
    model = compose.split("  llama-cpp:\n", 1)[1]

    api = compose.split("  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker = compose.split("  worker:\n", 1)[1].split("\n  llama-cpp:\n", 1)[0]

    assert "RUSA_MODEL_BASE_URL: http://llama-cpp:8080/v1" in api
    assert 'RUSA_LOCAL_MODEL_PATH: ""' in api
    assert 'RUSA_MODEL_PATH: ""' in api
    assert "RUSA_WORKER_INFERENCE_BASE_URL: http://api:8000/v1" in worker
    assert 'RUSA_WORKER_INFERENCE_GATEWAY: "true"' in worker
    assert "${RUSA_WORKER_INFERENCE_API_KEYS:?Set tenant-scoped worker inference API keys in .env}" in worker
    assert "RUSA_MODEL_BASE_URL: http://llama-cpp:8080/v1" not in worker
    assert "RUSA_MODEL_API_KEY: \"\"" in api
    assert "RUSA_MODEL_HEALTH_URL: http://llama-cpp:8080/health" in api
    assert "RUSA_MODEL_API_KEY: \"\"" in worker
    assert "${RUSA_WORKER_INFERENCE_DEADLINE_MS:-270000}" in worker
    assert "api:\n        condition: service_healthy" in worker
    assert "${RUSA_MAX_PARALLEL_WORKERS:-1}" in worker
    assert "${RUSA_MAX_CONCURRENT_INFERENCES:-1}" in compose
    assert "${RUSA_MAX_WAITING_INFERENCES:-3}" in compose
    assert "${RUSA_MODEL_NAME:-rusaai-cpu-smoke-test}" in compose
    assert "${RUSA_MODEL_NAME:-rusaai-edge-1.54b}" not in compose
    assert "${RUSA_MODEL_NAME:-rusa-cpu}" not in compose
    assert '"128"' in model
    assert '"64"' in model
    assert '"2"' in model
    assert "--threads-http" in model
    assert "${RUSA_GGUF_MODEL_PATH:?Set RUSA_GGUF_MODEL_PATH in .env}" in model
    assert "    ports:" not in model
    assert '      - --n-gpu-layers\n      - "0"' in model
    assert "--no-webui" not in model
    assert '      - "8080"' in model



def _memory_limit_mib(value: str) -> int:
    normalized = value.strip().lower()
    if normalized.endswith("g"):
        return int(float(normalized[:-1]) * 1024)
    if normalized.endswith("m"):
        return int(float(normalized[:-1]))
    raise AssertionError(f"Unsupported Compose memory unit: {value}")


def test_8gb_compose_profile_bounds_all_service_memory_and_waiting_work():
    cpu = yaml.safe_load((ROOT / "docker-compose.cpu.yml").read_text(encoding="utf-8"))
    overlay = yaml.safe_load(
        (ROOT / "docker-compose.cpu-8gb.yml").read_text(encoding="utf-8")
    )
    services = overlay["services"]

    assert set(services) == {"postgres", "api", "worker", "web", "llama-cpp"}
    assert services["postgres"]["mem_limit"] == "384m"
    assert services["api"]["mem_limit"] == "512m"
    assert services["worker"]["mem_limit"] == "768m"
    assert services["web"]["mem_limit"] == "384m"
    assert services["llama-cpp"]["mem_limit"] == "${RUSA_MODEL_MEMORY_LIMIT:-3g}"

    total_cap_mib = sum(
        _memory_limit_mib(
            services[name]["mem_limit"] if name != "llama-cpp" else "3g"
        )
        for name in services
    )
    assert total_cap_mib == 5 * 1024
    assert services["api"]["environment"]["RUSA_MAX_WAITING_INFERENCES"] == (
        "${RUSA_MAX_WAITING_INFERENCES:-1}"
    )
    assert services["worker"]["environment"]["RUSA_MAX_PARALLEL_WORKERS"] == (
        "${RUSA_MAX_PARALLEL_WORKERS:-1}"
    )
    env_values = {}
    for line in (ROOT / ".env.cpu-8gb.example").read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env_values[key] = value
    assert env_values["RUSA_CPU_THREADS"] == "2"
    assert env_values["RUSA_MODEL_MEMORY_LIMIT"] == "3g"
    assert env_values["RUSA_MODEL_CPU_LIMIT"] == "2.0"
    assert env_values["RUSA_MAX_WAITING_INFERENCES"] == "1"
    assert cpu["services"]["api"]["environment"]["RUSA_MAX_CONCURRENT_INFERENCES"] == (
        "${RUSA_MAX_CONCURRENT_INFERENCES:-1}"
    )
