import hashlib
import json
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

SERVE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve"))
if SERVE_DIR not in sys.path:
    sys.path.insert(0, SERVE_DIR)

import app as app_module
from app import create_app

KEY_A = "tenant-a-test-key-000000000000000000000000"
KEY_B = "tenant-b-test-key-000000000000000000000000"
READ_KEY = "readonly-test-key-000000000000000000000000"
DEV_KEY = "developer-test-key-000000000000000000000000"


def _key_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _registry():
    return {
        _key_hash(KEY_A): {"subject": "service-a", "tenant_id": "tenant-a", "roles": ["tenant_admin"]},
        _key_hash(KEY_B): {"subject": "service-b", "tenant_id": "tenant-b", "roles": ["tenant_admin"]},
        _key_hash(READ_KEY): {"subject": "readonly-a", "tenant_id": "tenant-a", "roles": ["read_only"]},
        _key_hash(DEV_KEY): {"subject": "developer-a", "tenant_id": "tenant-a", "roles": ["developer"]},
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUSA_API_KEYS_JSON", json.dumps(_registry()))
    monkeypatch.setenv("RUSA_RATE_LIMIT_MAX_REQUESTS", "100")
    monkeypatch.setenv("RUSA_RATE_LIMIT_WINDOW_SECONDS", "60")
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_is_public_and_minimal(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path,method,payload", [
    ("/v1/chat/completions", "post", {"messages": [{"role": "user", "content": "hello"}]}),
    ("/v1/validate-task", "post", {"envelope": {}}),
    ("/v1/records", "get", None),
])
def test_sensitive_endpoints_reject_missing_authentication(client, path, method, payload):
    response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
    assert response.status_code == 401


def test_invalid_api_key_is_rejected(client):
    response = client.post("/v1/chat/completions", headers={"X-API-Key": "x" * 40},
                           json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 401


def test_missing_auth_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    with TestClient(create_app()) as test_client:
        response = test_client.post("/v1/chat/completions",
                                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 503


def test_rbac_denies_read_only_role_from_generation(client):
    response = client.post("/v1/chat/completions", headers={"X-API-Key": READ_KEY},
                           json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 403


def test_rbac_denies_developer_from_writing_records(client):
    response = client.post("/v1/records", headers={"X-API-Key": DEV_KEY},
                           json={"data": {"label": "developer-write-attempt"}})
    assert response.status_code == 403


def test_unauthenticated_requests_are_rate_limited(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUSA_API_KEYS_JSON", json.dumps(_registry()))
    monkeypatch.setenv("RUSA_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("RUSA_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.delenv("RUSA_REDIS_URL", raising=False)
    with TestClient(create_app()) as test_client:
        headers = {"X-API-Key": "invalid-key-value-that-is-long-enough-000"}
        path = "/v1/chat/completions"
        assert test_client.post(path, headers=headers,
                                json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401
        assert test_client.post(path, headers=headers,
                                json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401
        response = test_client.post(path, headers=headers,
                                    json={"messages": [{"role": "user", "content": "x"}]})
    assert response.status_code == 429


def valid_envelope():
    return {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "human-1", "role": "human"},
        "to": {"agent_id": "rusa-1", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Run security regression tests",
            "acceptance_criteria": ["All tests pass"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "fix/security-hardening",
        },
        "policy": {
            "allowed_actions": ["read", "test"],
            "prohibited_actions": ["publish_secret"],
            "human_approval_required": ["merge", "deploy"],
        },
    }


def test_a2a_valid_envelope_passes_schema_validation(client):
    response = client.post("/v1/validate-task", headers={"X-API-Key": KEY_A},
                           json={"envelope": valid_envelope()})
    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


def test_a2a_invalid_envelope_is_rejected(client):
    response = client.post("/v1/validate-task", headers={"X-API-Key": KEY_A},
                           json={"envelope": {"protocol_version": "1.0"}})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"]


def test_missing_a2a_schema_fails_closed(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "A2A_SCHEMA_PATH", tmp_path / "missing-schema.json")
    response = client.post("/v1/validate-task", headers={"X-API-Key": KEY_A},
                           json={"envelope": valid_envelope()})
    assert response.status_code == 503
    assert response.json()["detail"] == "A2A schema is unavailable"


def test_request_validation_does_not_echo_sensitive_input(client, caplog):
    secret = "super-secret-value-should-not-be-echoed"
    response = client.post("/v1/chat/completions", headers={"X-API-Key": KEY_A},
                           json={"messages": [{"role": "invalid", "content": secret}]})
    assert response.status_code == 422
    assert secret not in response.text
    assert secret not in caplog.text


def test_request_size_limit_rejects_oversized_body(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUSA_API_KEYS_JSON", json.dumps(_registry()))
    with TestClient(create_app(max_request_bytes=128)) as test_client:
        response = test_client.post("/v1/chat/completions", headers={"X-API-Key": KEY_A},
                                    json={"messages": [{"role": "user", "content": "x" * 1000}]})
    assert response.status_code == 413


def test_rate_limit_blocks_excess_requests(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUSA_API_KEYS_JSON", json.dumps(_registry()))
    monkeypatch.setenv("RUSA_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("RUSA_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.delenv("RUSA_REDIS_URL", raising=False)
    with TestClient(create_app()) as test_client:
        headers = {"X-API-Key": KEY_A}
        payload = {"envelope": {"bad": True}}
        assert test_client.post("/v1/validate-task", headers=headers, json=payload).status_code == 200
        assert test_client.post("/v1/validate-task", headers=headers, json=payload).status_code == 200
        response = test_client.post("/v1/validate-task", headers=headers, json=payload)
    assert response.status_code == 429


def test_chat_endpoint_uses_mock_only_in_nonproduction(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUSA_API_KEYS_JSON", json.dumps(_registry()))
    with TestClient(create_app()) as test_client:
        response = test_client.post("/v1/chat/completions", headers={"X-API-Key": KEY_A},
                                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"].startswith("[development mock]")


def test_production_readiness_is_blocked_until_real_inference_exists(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    readiness = client.get("/ready")
    completion = client.post("/v1/chat/completions", headers={"X-API-Key": KEY_A},
                             json={"messages": [{"role": "user", "content": "hello"}]})
    assert readiness.status_code == 503
    assert completion.status_code == 503
    assert "mock" not in completion.text.lower()


@pytest.mark.skipif(not os.getenv("RUSA_DATABASE_URL"), reason="PostgreSQL integration service is not configured")
def test_postgres_rls_prevents_cross_tenant_reads(client):
    created = client.post("/v1/records", headers={"X-API-Key": KEY_A}, json={"data": {"label": "tenant-a"}})
    assert created.status_code == 201
    record_id = created.json()["id"]
    assert any(item["id"] == record_id for item in client.get("/v1/records", headers={"X-API-Key": KEY_A}).json()["items"])
    assert client.get("/v1/records", headers={"X-API-Key": KEY_B}).json()["items"] == []
    assert client.get(f"/v1/records/{record_id}", headers={"X-API-Key": KEY_B}).status_code == 404


@pytest.mark.skipif(not os.getenv("RUSA_DATABASE_URL"), reason="PostgreSQL integration service is not configured")
def test_postgres_rls_rejects_cross_tenant_insert(client):
    import psycopg
    conn = psycopg.connect(os.environ["RUSA_DATABASE_URL"])
    try:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", ("tenant-a",))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO public.tenant_records (tenant_id, owner_subject, payload) "
                "VALUES (%s, %s, %s::jsonb)",
                ("tenant-b", "attacker", '{"attempt":"cross-tenant"}'),
            )
    finally:
        conn.close()


@pytest.mark.skipif(not os.getenv("RUSA_DATABASE_URL"), reason="PostgreSQL integration service is not configured")
def test_tenant_id_cannot_be_overridden_by_request(client):
    response = client.post("/v1/records", headers={"X-API-Key": KEY_A},
                           json={"data": {"label": "x"}, "tenant_id": "tenant-b"})
    assert response.status_code == 422


@pytest.mark.skipif(not os.getenv("RUSA_DATABASE_URL"), reason="PostgreSQL integration service is not configured")
def test_sql_injection_record_id_is_rejected_before_query(client):
    response = client.get("/v1/records/00000000-0000-0000-0000-000000000000%20OR%201%3D1",
                          headers={"X-API-Key": KEY_A})
    assert response.status_code == 422
