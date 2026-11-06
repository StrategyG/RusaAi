import asyncio
import os
import sys
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")),
)

from app import (  # noqa: E402
    GenerationEvent,
    GenerationResult,
    InferenceAdmissionController,
    TransientInferenceError,
    app,
)

TEST_API_KEY = "test-key-that-is-long-enough-for-production-policy-123"


class FakeBackend:
    name = "test-backend"
    device = "cpu"
    supports_batching = False

    async def generate(self, messages, *, max_new_tokens, temperature, top_p):
        assert messages[-1]["content"] == "Hello RUSA"
        assert max_new_tokens == 64
        return GenerationResult(
            content="Hello from a test inference backend.",
            prompt_tokens=12,
            completion_tokens=8,
            finish_reason="stop",
        )

    async def stream(self, messages, *, max_new_tokens, temperature, top_p):
        assert messages[-1]["content"] == "Hello RUSA"
        yield GenerationEvent(delta="Hello from ")
        yield GenerationEvent(delta="a streamed test backend.")
        yield GenerationEvent(
            finish_reason="stop",
            prompt_tokens=12,
            completion_tokens=8,
        )

    async def close(self):
        return None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RUSA_API_KEY", TEST_API_KEY)
    monkeypatch.delenv("RUSA_MODEL_PATH", raising=False)
    monkeypatch.setenv("RUSA_MODEL_NAME", "rusa-ai-v1")
    monkeypatch.delenv("TASK_ENVELOPE_SCHEMA_PATH", raising=False)

    with TestClient(app) as test_client:
        app.state.backend = FakeBackend()
        app.state.artifact = object()
        yield test_client
        app.state.backend = None
        app.state.artifact = None


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


def test_readiness_requires_model_and_artifact(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["model_loaded"] is True
    assert response.json()["checks"]["model_manifest_verified"] is True


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


def test_streaming_emits_openai_sse_chunks_and_usage(client):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "messages": [{"role": "user", "content": "Hello RUSA"}],
            "stream": True,
            "max_tokens": 64,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"object":"chat.completion.chunk"' in response.text
    assert '"content":"Hello from "' in response.text
    assert '"content":"a streamed test backend."' in response.text
    assert '"completion_tokens":8' in response.text
    assert "data: [DONE]" in response.text


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


def test_model_list_requires_auth_and_reports_loaded_model(client):
    response = client.get("/v1/models", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "rusa-ai-v1"
    assert client.get("/v1/models").status_code == 401


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


def test_admission_controller_rejects_excess_queue():
    async def exercise():
        controller = InferenceAdmissionController(
            max_active=1,
            max_waiting=0,
            queue_timeout=0.1,
        )
        await controller.acquire()
        try:
            with pytest.raises(HTTPException) as error:
                await controller.acquire()
            assert error.value.status_code == 429
        finally:
            controller.release()

    asyncio.run(exercise())


def test_generation_timeout_returns_gateway_timeout(client, monkeypatch):
    class SlowBackend(FakeBackend):
        async def generate(self, *args, **kwargs):
            await asyncio.sleep(0.1)
            return await super().generate(*args, **kwargs)

    app.state.backend = SlowBackend()
    monkeypatch.setenv("RUSA_GENERATION_TIMEOUT_SECONDS", "0.01")
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"messages": [{"role": "user", "content": "Hello RUSA"}]},
    )
    assert response.status_code == 504


def test_transient_inference_failure_is_retried_once(client, monkeypatch):
    class FlakyBackend(FakeBackend):
        def __init__(self):
            self.attempts = 0

        async def generate(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise TransientInferenceError("temporary backend transport failure")
            return await super().generate(*args, **kwargs)

    backend = FlakyBackend()
    app.state.backend = backend
    monkeypatch.setenv("RUSA_INFERENCE_RETRIES", "1")
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"messages": [{"role": "user", "content": "Hello RUSA"}]},
    )
    assert response.status_code == 200
    assert backend.attempts == 2


def test_admission_controller_never_exceeds_active_limit():
    async def exercise():
        controller = InferenceAdmissionController(
            max_active=2,
            max_waiting=4,
            queue_timeout=1.0,
        )
        active = 0
        peak_active = 0

        async def worker():
            nonlocal active, peak_active
            await controller.acquire()
            try:
                active += 1
                peak_active = max(peak_active, active)
                await asyncio.sleep(0.01)
            finally:
                active -= 1
                controller.release()

        await asyncio.gather(*(worker() for _ in range(6)))
        assert peak_active <= 2

    asyncio.run(exercise())


def test_request_body_limit_rejects_oversized_payload(client):
    oversized_body = b"x" * (1024 * 1024 + 1)
    response = client.post(
        "/v1/chat/completions",
        headers={**auth_headers(), "Content-Type": "application/json"},
        content=oversized_body,
    )
    assert response.status_code == 413
    assert response.headers["Connection"] == "close"
