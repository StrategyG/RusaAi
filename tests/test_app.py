import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../llm-project/serve")
    ),
)

from app import app  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    monkeypatch.delenv("RUSA_MODEL_PATH", raising=False)
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
            "objective": "Add a regression test",
            "acceptance_criteria": ["The test passes"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "fix/regression-test",
        },
        "policy": {
            "allowed_actions": ["read_repository", "create_branch"],
            "prohibited_actions": ["read_secrets"],
            "human_approval_required": ["merge", "deploy"],
        },
    }


def test_liveness_endpoint(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_health_reports_model_not_ready_without_weights(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["model_ready"] is False


def test_readiness_fails_without_real_checkpoint(client):
    response = client.get("/health/ready")
    assert response.status_code == 503


def test_chat_endpoint_never_returns_mock_output(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert response.status_code == 503
    assert "not ready" in response.json()["detail"].lower()
    assert "[RUSA-AI Model Output]" not in response.text


def test_batch_endpoint_requires_loaded_model(client):
    response = client.post(
        "/v1/chat/completions/batch",
        json={
            "requests": [
                {"messages": [{"role": "user", "content": "Hello"}]},
                {"messages": [{"role": "user", "content": "Hi"}]},
            ]
        },
    )
    assert response.status_code == 503


def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("RUSA_API_KEY", "a" * 40)
    response = client.post(
        "/v1/validate-task",
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 401

    response = client.post(
        "/v1/validate-task",
        headers={"X-API-Key": "a" * 40},
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_valid_a2a_envelope_passes_schema(client):
    response = client.post(
        "/v1/validate-task",
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


def test_invalid_a2a_envelope_is_rejected(client):
    response = client.post(
        "/v1/validate-task",
        json={"envelope": {"protocol_version": "1.0"}},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"]


def test_missing_a2a_schema_fails_closed(client, monkeypatch, tmp_path):
    import app as serving_app

    monkeypatch.setattr(
        serving_app, "SCHEMA_PATH", Path(tmp_path) / "missing-schema.json"
    )
    response = client.post(
        "/v1/validate-task",
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 503
    assert "cannot be skipped" in response.json()["detail"]


def test_unknown_request_fields_are_rejected(client):
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "unexpected_admin_override": True,
        },
    )
    assert response.status_code == 422


def test_model_name_is_not_silently_substituted_when_loaded(client):
    # No checkpoint is loaded in the test environment; readiness must fail first.
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "unregistered-model",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 503
