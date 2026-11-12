import hashlib
import json

import pytest

from rusa_agents.config import RuntimeConfig


def set_api_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "RUSA_API_DATABASE_URL",
        "postgresql://rusa_api_login:secret@db/rusa?sslmode=verify-full",
    )
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        '{"e33cdf9c7f7120b98e8c78408953e07f2ecd183006b5606df349b4c212acf43e":'
        '{"subject":"api-test","tenant_id":"tenant-a","roles":["tenant_admin"]}}',
    )
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 40)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/v1")
    monkeypatch.setenv("RUSA_MODEL_NAME", "rusaai-edge-derived")
    monkeypatch.setenv("RUSA_MODEL_API_KEY", "r" * 48)
    monkeypatch.setenv("RUSA_MODEL_TELEMETRY_URL", "https://model.internal/v1/telemetry")
    monkeypatch.setenv("RUSA_MODEL_TELEMETRY_API_KEY", "t" * 48)


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
    monkeypatch.setenv("RUSA_MODEL_NAME", "rusaai-edge-derived")
    monkeypatch.setenv("RUSA_MODEL_API_KEY", "r" * 48)
    worker_key = "w" * 48
    worker_key_digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        json.dumps({
            worker_key_digest: {
                "subject": "rusa-worker-tenant-a",
                "tenant_id": "tenant-a",
                "roles": ["developer"],
            }
        }),
    )
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_GATEWAY", "true")
    monkeypatch.setenv(
        "RUSA_WORKER_INFERENCE_BASE_URL", "https://api.internal/v1"
    )
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_CONTRACT", "true")
    monkeypatch.setenv(
        "RUSA_WORKER_INFERENCE_CONTRACT_URL",
        "https://api.internal/internal/v1/inference",
    )
    monkeypatch.setenv(
        "RUSA_WORKER_INFERENCE_API_KEYS", json.dumps({"tenant-a": worker_key})
    )


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


def test_production_api_rejects_legacy_raw_api_key_registry(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.setenv("RUSA_API_KEYS", '{"' + "a" * 40 + '":"tenant-a"}')
    with pytest.raises(RuntimeError, match="legacy RUSA_API_KEYS"):
        RuntimeConfig.from_env().validate_api()


def test_api_key_registry_rejects_unknown_roles(monkeypatch):
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        '{"' + "a" * 64 + '":{"subject":"api-test","tenant_id":"tenant-a","roles":["root"]}}',
    )
    with pytest.raises(ValueError, match="roles"):
        RuntimeConfig.from_env()


def test_production_api_rejects_plain_http_model_endpoint(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "http://llama-cpp:8080/v1")

    with pytest.raises(RuntimeError, match="credential-free HTTPS"):
        RuntimeConfig.from_env().validate_api()


def test_production_worker_rejects_plain_http_model_endpoint(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "http://llama-cpp:8080/v1")

    with pytest.raises(RuntimeError, match="credential-free HTTPS"):
        RuntimeConfig.from_env().validate_worker()


def test_production_api_rejects_missing_model_gateway_token(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.delenv("RUSA_MODEL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="RUSA_MODEL_API_KEY"):
        RuntimeConfig.from_env().validate_api()


def test_production_worker_does_not_receive_model_gateway_secret(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.delenv("RUSA_MODEL_API_KEY", raising=False)
    RuntimeConfig.from_env().validate_worker()


def test_production_worker_rejects_missing_tenant_inference_credential(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_API_KEYS", "{}")
    with pytest.raises(RuntimeError, match="missing tenant tenant-a"):
        RuntimeConfig.from_env().validate_worker()


def test_production_worker_rejects_inference_key_not_bound_to_tenant(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_API_KEYS", json.dumps({"tenant-a": "x" * 48}))
    with pytest.raises(RuntimeError, match="tenant-bound inference principal"):
        RuntimeConfig.from_env().validate_worker()


def test_production_worker_rejects_unknown_inference_key_tenants(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv(
        "RUSA_WORKER_INFERENCE_API_KEYS",
        json.dumps({"tenant-a": "w" * 48, "tenant-b": "x" * 48}),
    )
    with pytest.raises(RuntimeError, match="outside RUSA_TENANT_REPOSITORIES"):
        RuntimeConfig.from_env().validate_worker()


def test_production_worker_requires_tls_api_gateway(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_BASE_URL", "http://api:8000/v1")
    with pytest.raises(RuntimeError, match="RUSA_WORKER_INFERENCE_BASE_URL"):
        RuntimeConfig.from_env().validate_worker()



def test_default_model_name_uses_safe_smoke_alias_until_checkpoint_is_approved(monkeypatch):
    monkeypatch.delenv("RUSA_MODEL_NAME", raising=False)
    assert RuntimeConfig.from_env().model_name == "rusaai-cpu-smoke-test"



def test_production_api_rejects_missing_model_telemetry_credentials(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.delenv("RUSA_MODEL_TELEMETRY_URL", raising=False)
    monkeypatch.delenv("RUSA_MODEL_TELEMETRY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="RUSA_MODEL_TELEMETRY_URL"):
        RuntimeConfig.from_env().validate_api()


def test_production_api_rejects_untrusted_model_telemetry_endpoint(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.setenv("RUSA_MODEL_TELEMETRY_URL", "http://model.internal/v1/telemetry")

    with pytest.raises(RuntimeError, match="credential-free HTTPS"):
        RuntimeConfig.from_env().validate_api()


def test_production_runtime_rejects_unapproved_smoke_model_identity(monkeypatch):
    set_api_environment(monkeypatch)
    monkeypatch.setenv("RUSA_MODEL_NAME", "rusaai-cpu-smoke-test")

    with pytest.raises(RuntimeError, match="smoke-test alias"):
        RuntimeConfig.from_env().validate_api()



def test_production_worker_rejects_missing_inference_contract_url(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.delenv("RUSA_WORKER_INFERENCE_CONTRACT_URL", raising=False)

    with pytest.raises(RuntimeError, match="RUSA_WORKER_INFERENCE_CONTRACT_URL"):
        RuntimeConfig.from_env().validate_worker()


def test_production_worker_rejects_openai_only_gateway_mode(monkeypatch):
    set_worker_environment(monkeypatch)
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_CONTRACT", "false")

    with pytest.raises(RuntimeError, match="CONTRACT=true"):
        RuntimeConfig.from_env().validate_worker()
