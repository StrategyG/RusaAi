from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_production_worker_uses_tenant_scoped_tls_api_gateway():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    worker = compose["services"]["worker"]
    environment = worker["environment"]

    assert environment["RUSA_WORKER_INFERENCE_GATEWAY"] == "true"
    assert environment["RUSA_WORKER_INFERENCE_BASE_URL"] == (
        "${RUSA_WORKER_INFERENCE_BASE_URL:?Set the trusted HTTPS API gateway URL}"
    )
    assert environment["RUSA_WORKER_INFERENCE_API_KEYS"] == (
        "${RUSA_WORKER_INFERENCE_API_KEYS:?Set tenant-scoped worker inference keys}"
    )
    assert environment["RUSA_API_KEYS_JSON"] == (
        "${RUSA_API_KEYS_JSON:?Set the SHA-256 tenant API key registry}"
    )
    assert environment["RUSA_MODEL_API_KEY"] == ""
    assert worker["depends_on"]["api"]["condition"] == "service_healthy"
    assert "rusa-internal" in worker["networks"]


def test_production_worker_does_not_receive_api_metrics_or_model_secrets():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["worker"]["environment"]

    assert environment["RUSA_METRICS_API_KEY"] == ""
    assert environment["RUSA_MODEL_API_KEY"] == ""
    assert "RUSA_WORKER_INFERENCE_API_KEYS" in environment
