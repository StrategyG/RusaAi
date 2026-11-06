import os
import sys
import uuid
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")))

from app import app

API_KEY = "test-only-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_API_KEY", API_KEY)
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("key", [None, "", " \t"])
def test_production_startup_requires_api_key(monkeypatch, key):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    if key is None:
        monkeypatch.delenv("RUSA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("RUSA_API_KEY", key)

    with pytest.raises(RuntimeError, match=r"^RUSA_API_KEY must be configured in production\.$"):
        with TestClient(app):
            pass


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.0.0",
        "environment": "production",
    }


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "Hello"}]}),
        ("/v1/validate-task", {"envelope": {}}),
    ],
)
@pytest.mark.parametrize("key", [None, "incorrect-api-key", b"\xff"])
def test_protected_endpoints_reject_missing_or_invalid_key(client, path, payload, key):
    headers = {} if key is None else {"X-API-Key": key}
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API Key"}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/validate-task"])
def test_protected_endpoints_fail_closed_without_configured_key(monkeypatch, path):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEY", raising=False)
    with TestClient(app) as client:
        response = client.post(path, json={}, headers=AUTH_HEADERS)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or missing API Key"}


def test_chat_completions_endpoint(client):
    payload = {
        "messages": [
            {"role": "user", "content": "Hello"}
        ]
    }
    response = client.post("/v1/chat/completions", json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data


def test_validate_task_endpoint_valid(client):
    valid_envelope = {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "agent-a", "role": "chatgpt_app"},
        "to": {"agent_id": "agent-b", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Test Task",
            "acceptance_criteria": ["Task is valid"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "fix/test",
        },
        "policy": {
            "allowed_actions": [],
            "prohibited_actions": [],
            "human_approval_required": [],
        },
    }
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope}, headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True


def test_validate_task_endpoint_invalid(client):
    invalid_envelope = {
        "protocol_version": "1.0"
    }
    response = client.post("/v1/validate-task", json={"envelope": invalid_envelope}, headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
