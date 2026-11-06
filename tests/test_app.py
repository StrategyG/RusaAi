import importlib.util
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location(
    "rusa_serve", Path(__file__).resolve().parents[1] / "llm-project/serve/app.py"
)
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)
client = TestClient(serve.app)


@pytest.fixture(autouse=True)
def configured_key(monkeypatch):
    monkeypatch.setenv("RUSA_API_KEY", "test-only-key")


@pytest.fixture
def envelope():
    return {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()), "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "planner", "role": "rusa_orchestrator"},
        "to": {"agent_id": "worker", "role": "github_copilot"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Validate contract", "acceptance_criteria": ["Tests pass"],
            "repository": "StrategyG/RusaAi", "base_branch": "main",
            "work_branch": "fix/contract"
        },
        "policy": {
            "allowed_actions": ["read"], "prohibited_actions": ["deploy"],
            "human_approval_required": ["merge", "deploy"]
        }
    }


def post(envelope, headers=None):
    return client.post("/v1/validate-task", json={"envelope": envelope},
                       headers=headers if headers is not None else {"X-API-Key": "test-only-key"})


def test_liveness_is_separate_from_readiness():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").status_code == 503


def test_valid_envelope(envelope):
    response = post(envelope)
    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


def test_invalid_envelope():
    assert post({"protocol_version": "1.0"}).json()["valid"] is False


@pytest.mark.parametrize("field,value", [
    ("task_id", "not-uuid"), ("message_id", "not-uuid"),
    ("created_at", "not-date"), ("from", "agent-a"), ("type", "unknown")
])
def test_invalid_contract_fields(envelope, field, value):
    envelope[field] = value
    assert post(envelope).json()["valid"] is False


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_unauthorized(envelope, headers):
    assert post(envelope, headers).status_code == 401


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_configuration_fails_closed(envelope, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("RUSA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("RUSA_API_KEY", key)
    assert post(envelope).status_code == 503


@pytest.mark.parametrize("contents", [None, "{", '{"type": 123}'])
def test_schema_unavailable_fails_closed(envelope, monkeypatch, tmp_path, contents):
    path = tmp_path / "schema.json"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setattr(serve, "SCHEMA_PATH", path)
    response = post(envelope)
    assert response.status_code == 503
    assert str(tmp_path) not in response.text


def test_errors_do_not_echo_payload(envelope):
    envelope["private-token"] = "secret-value"
    response = post(envelope)
    assert response.json()["valid"] is False
    assert "secret-value" not in response.text
    assert "private-token" not in response.text


@pytest.mark.parametrize("messages", [[], [{"role": "bad", "content": "hello"}],
                                       [{"role": "user", "content": ""}]])
def test_invalid_chat_input(messages):
    response = client.post("/v1/chat/completions", json={"messages": messages},
                           headers={"X-API-Key": "test-only-key"})
    assert response.status_code == 422


def test_inference_never_returns_mock_success():
    response = client.post("/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "hello"}]},
                           headers={"X-API-Key": "test-only-key"})
    assert response.status_code == 503
    assert "choices" not in response.json()


def test_chat_requires_authentication():
    response = client.post("/v1/chat/completions",
                           json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 401
