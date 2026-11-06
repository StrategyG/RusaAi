import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")),
)

from app import GenerationResult, _load_task_validator, app  # noqa: E402

TEST_API_KEY = "test-key-that-is-long-enough-for-production-policy-123"


class FakeBackend:
    def generate(
        self,
        messages,
        *,
        max_new_tokens,
        temperature,
        top_p,
    ):
        assert messages[-1].content == "Hello RUSA"
        assert max_new_tokens == 64
        return GenerationResult(
            content="Hello from a test inference backend.",
            prompt_tokens=12,
            completion_tokens=8,
            finish_reason="stop",
        )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RUSA_API_KEY", TEST_API_KEY)
    monkeypatch.delenv("RUSA_MODEL_PATH", raising=False)
    monkeypatch.setenv("RUSA_MODEL_NAME", "rusa-ai-v1")
    monkeypatch.delenv("TASK_ENVELOPE_SCHEMA_PATH", raising=False)
    _load_task_validator.cache_clear()

    with TestClient(app) as test_client:
        app.state.backend = FakeBackend()
        yield test_client
        app.state.backend = None

    _load_task_validator.cache_clear()


def auth_headers():
    return {"X-API-Key": TEST_API_KEY}


def valid_envelope():
    return {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "human-1", "role": "human"},
        "to": {"agent_id": "rusa-main", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Run a production readiness test",
            "acceptance_criteria": ["All required tests pass"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "feature/readiness-test",
        },
        "policy": {
            "allowed_actions": ["read_repository"],
            "prohibited_actions": ["deploy"],
            "human_approval_required": ["merge", "deploy"],
        },
    }


def test_liveness_endpoint_is_public(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_legacy_health_endpoint_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "rusa-ai-serve"


def test_readiness_reports_missing_model(client):
    app.state.backend = None
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["model_loaded"] is False


def test_chat_completion_uses_backend_and_returns_usage(client):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "model": "rusa-ai-v1",
            "messages": [{"role": "user", "content": "Hello RUSA"}],
            "max_tokens": 64,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == (
        "Hello from a test inference backend."
    )
    assert data["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
    }


def test_chat_completion_requires_api_key(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello RUSA"}]},
    )
    assert response.status_code == 401


def test_missing_api_key_configuration_fails_closed(client, monkeypatch):
    monkeypatch.delenv("RUSA_API_KEY")
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"messages": [{"role": "user", "content": "Hello RUSA"}]},
    )
    assert response.status_code == 503


def test_chat_completion_returns_503_when_model_is_not_loaded(client):
    app.state.backend = None
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"messages": [{"role": "user", "content": "Hello RUSA"}]},
    )
    assert response.status_code == 503


def test_streaming_is_not_silently_ignored(client):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "messages": [{"role": "user", "content": "Hello RUSA"}],
            "stream": True,
        },
    )
    assert response.status_code == 501


def test_unknown_model_is_rejected(client):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "model": "unknown-model",
            "messages": [{"role": "user", "content": "Hello RUSA"}],
        },
    )
    assert response.status_code == 404


def test_invalid_chat_role_is_rejected(client):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"messages": [{"role": "tool", "content": "Hello RUSA"}]},
    )
    assert response.status_code == 422


def test_valid_a2a_envelope_passes_schema_validation(client):
    response = client.post(
        "/v1/validate-task",
        headers=auth_headers(),
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


def test_invalid_a2a_envelope_fails_schema_validation(client):
    response = client.post(
        "/v1/validate-task",
        headers=auth_headers(),
        json={"envelope": {"protocol_version": "1.0"}},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"]


def test_a2a_schema_missing_fails_closed(client, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "TASK_ENVELOPE_SCHEMA_PATH",
        str(tmp_path / "missing-schema.json"),
    )
    _load_task_validator.cache_clear()

    response = client.post(
        "/v1/validate-task",
        headers=auth_headers(),
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 503


def test_a2a_validation_requires_api_key(client):
    response = client.post(
        "/v1/validate-task",
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 401


def test_agent_task_submission_is_authenticated_and_idempotent(client):
    class FakeQueue:
        def __init__(self):
            self.calls = []

        def enqueue(self, tenant_id, task_id, payload, idempotency_key):
            self.calls.append((tenant_id, task_id, payload, idempotency_key))
            return {"task_id": task_id, "state": "queued"}

    queue = FakeQueue()
    app.state.task_queue = queue
    response = client.post(
        "/v1/agent/tasks",
        headers={**auth_headers(), "Idempotency-Key": "task-key-0001"},
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    assert len(queue.calls) == 1
    assert queue.calls[0][0] == "default"
    assert queue.calls[0][3] == "task-key-0001"


def test_agent_task_submission_requires_authentication(client):
    response = client.post(
        "/v1/agent/tasks",
        headers={"Idempotency-Key": "task-key-0002"},
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 401


def test_agent_task_submission_fails_closed_without_queue(client):
    app.state.task_queue = None
    response = client.post(
        "/v1/agent/tasks",
        headers={**auth_headers(), "Idempotency-Key": "task-key-0003"},
        json={"envelope": valid_envelope()},
    )
    assert response.status_code == 503


def test_agent_task_submission_rejects_non_create_envelope(client):
    class FakeQueue:
        def enqueue(self, *args):
            raise AssertionError("queue must not receive non-create events")

    app.state.task_queue = FakeQueue()
    envelope = valid_envelope()
    envelope["type"] = "task.cancel"
    response = client.post(
        "/v1/agent/tasks",
        headers={**auth_headers(), "Idempotency-Key": "task-key-0004"},
        json={"envelope": envelope},
    )
    assert response.status_code == 422
