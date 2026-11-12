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

    assert "RUSA_MODEL_HEALTH_URL" not in api_environment
    assert "env_file" not in api
    assert "env_file" not in worker
    assert "env_file" not in compose["services"]["web"]
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


def test_cpu_compose_routes_api_through_rusamodels_model_plane():
    compose = yaml.safe_load((ROOT / "docker-compose.cpu.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    api_environment = services["api"]["environment"]
    worker = services["worker"]
    worker_environment = worker["environment"]

    assert "llama-cpp" not in services
    assert api_environment["RUSA_MODEL_BASE_URL"].startswith(
        "${RUSA_MODEL_BASE_URL:?"
    )
    assert api_environment["RUSA_MODEL_API_KEY"].startswith(
        "${RUSA_MODEL_API_KEY:?"
    )
    assert api_environment["RUSA_MODEL_NAME"] == "${RUSA_MODEL_NAME:-rusa-general-4b}"
    assert api_environment["RUSA_LOCAL_MODEL_PATH"] == ""
    assert api_environment["RUSA_MODEL_PATH"] == ""
    assert "RUSA_MODEL_HEALTH_URL" not in api_environment
    assert worker_environment["RUSA_WORKER_INFERENCE_BASE_URL"] == "http://api:8000/v1"
    assert worker_environment["RUSA_WORKER_INFERENCE_GATEWAY"] == "true"
    assert worker_environment["RUSA_MODEL_API_KEY"] == ""
    assert worker["depends_on"]["api"]["condition"] == "service_healthy"
    assert worker_environment["RUSA_MODEL_NAME"] == "${RUSA_MODEL_NAME:-rusa-general-4b}"
    assert "${RUSA_WORKER_INFERENCE_API_KEYS:?" in worker_environment[
        "RUSA_WORKER_INFERENCE_API_KEYS"
    ]
    assert "${RUSA_WORKER_INFERENCE_DEADLINE_MS:-270000}" == worker_environment[
        "RUSA_WORKER_INFERENCE_DEADLINE_MS"
    ]
    assert api_environment["RUSA_MAX_CONCURRENT_INFERENCES"] == (
        "${RUSA_MAX_CONCURRENT_INFERENCES:-1}"
    )
    assert api_environment["RUSA_MAX_WAITING_INFERENCES"] == (
        "${RUSA_MAX_WAITING_INFERENCES:-3}"
    )
    assert "RUSA_GGUF_MODEL_PATH" not in str(compose)
    assert "RUSA_MODEL_CONTEXT" not in str(compose)
    assert "RUSA_CPU_THREADS" not in str(compose)


def _memory_limit_mib(value: str) -> int:
    normalized = value.strip().lower()
    if normalized.endswith("g"):
        return int(float(normalized[:-1]) * 1024)
    if normalized.endswith("m"):
        return int(float(normalized[:-1]))
    raise AssertionError(f"Unsupported Compose memory unit: {value}")


def test_8gb_compose_profile_bounds_control_plane_memory_and_waiting_work():
    cpu = yaml.safe_load((ROOT / "docker-compose.cpu.yml").read_text(encoding="utf-8"))
    overlay = yaml.safe_load(
        (ROOT / "docker-compose.cpu-8gb.yml").read_text(encoding="utf-8")
    )
    services = overlay["services"]

    assert set(services) == {"postgres", "api", "worker", "web"}
    assert services["postgres"]["mem_limit"] == "384m"
    assert services["api"]["mem_limit"] == "512m"
    assert services["worker"]["mem_limit"] == "768m"
    assert services["web"]["mem_limit"] == "384m"

    total_cap_mib = sum(_memory_limit_mib(service["mem_limit"]) for service in services.values())
    assert total_cap_mib == 2 * 1024

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
    assert env_values["RUSA_MAX_WAITING_INFERENCES"] == "1"
    assert env_values["RUSA_MAX_CONCURRENT_INFERENCES"] == "1"
    assert env_values["RUSA_MAX_PARALLEL_WORKERS"] == "1"
    assert "RUSA_CPU_THREADS" not in env_values
    assert "RUSA_MODEL_MEMORY_LIMIT" not in env_values
    assert cpu["services"]["api"]["environment"]["RUSA_MAX_CONCURRENT_INFERENCES"] == (
        "${RUSA_MAX_CONCURRENT_INFERENCES:-1}"
    )
