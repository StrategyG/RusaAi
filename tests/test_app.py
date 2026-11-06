import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "llm-project" / "serve"))

import app as serving_app
from app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RUSA_ENABLE_MOCK_INFERENCE", "true")
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    with TestClient(app) as test_client:
        yield test_client


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
            "objective": "Validate the task envelope",
            "acceptance_criteria": ["The envelope passes schema validation"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "docs/readme-developer-setup",
        },
        "policy": {
            "allowed_actions": ["read", "test"],
            "prohibited_actions": ["deploy"],
            "human_approval_required": ["merge", "release"],
        },
    }


def test_health_endpoint_returns_liveness_contract(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["inference"] == "not_configured"


def test_chat_completion_uses_explicit_development_mock(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert response.status_code == 200
    assert "[DEVELOPMENT MOCK ONLY]" in response.json()["choices"][0]["message"]["content"]


def test_chat_completion_returns_503_without_inference_backend(client, monkeypatch):
    monkeypatch.setenv("RUSA_ENABLE_MOCK_INFERENCE", "false")
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Inference backend is not configured."


def test_streaming_is_rejected_until_sse_is_implemented(client):
    response = client.post(
        "/v1/chat/completions",
        json={
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 501


def test_chat_and_validation_require_api_key_when_configured(client, monkeypatch):
    monkeypatch.setenv("RUSA_API_KEY", "test-secret-key-with-more-than-32-characters")
    chat_payload = {"messages": [{"role": "user", "content": "Hello"}]}

    assert client.post("/v1/chat/completions", json=chat_payload).status_code == 401
    assert client.post(
        "/v1/chat/completions",
        json=chat_payload,
        headers={"X-API-Key": "test-secret-key-with-more-than-32-characters"},
    ).status_code == 200

    assert client.post(
        "/v1/validate-task",
        json={"envelope": valid_envelope()},
    ).status_code == 401


def test_validate_task_endpoint_accepts_schema_conformant_envelope(client):
    response = client.post(
        "/v1/validate-task",
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


def test_validate_task_rejects_invalid_uuid_and_date_time(client):
    envelope = valid_envelope()
    envelope["task_id"] = "not-a-uuid"
    envelope["created_at"] = "not-a-date-time"

    response = client.post(
        "/v1/validate-task",
        json={"envelope": envelope},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    errors = response.json()["errors"]
    assert any("task_id" in error and "uuid" in error for error in errors)
    assert any("created_at" in error and "date-time" in error for error in errors)
    assert all("not-a-uuid" not in error for error in errors)
    assert all("not-a-date-time" not in error for error in errors)


def test_validate_task_rejects_missing_required_fields(client):
    response = client.post(
        "/v1/validate-task",
        json={"envelope": {"protocol_version": "1.0"}},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"]


def test_missing_schema_fails_application_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    monkeypatch.setattr(serving_app, "TASK_SCHEMA_PATH", tmp_path / "missing.json")

    with pytest.raises(RuntimeError, match="Unable to load required task schema"):
        with TestClient(app):
            pass


def test_invalid_schema_fails_application_startup(monkeypatch, tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"not-a-type"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    monkeypatch.setattr(serving_app, "TASK_SCHEMA_PATH", schema_path)

    with pytest.raises(RuntimeError, match="Invalid task schema"):
        with TestClient(app):
            pass


def test_production_startup_requires_strong_api_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    monkeypatch.setenv("RUSA_ENABLE_MOCK_INFERENCE", "false")

    with pytest.raises(RuntimeError, match="RUSA_API_KEY must contain at least 32 characters"):
        with TestClient(app):
            pass
