import os
import sys
import uuid
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")))

import app as serving

@pytest.fixture
def client():
    with TestClient(serving.app) as client:
        yield client


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_chat_completions_endpoint(client):
    payload = {
        "messages": [
            {"role": "user", "content": "Hello"}
        ]
    }
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data


@pytest.fixture
def valid_envelope():
    return {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "agent-a", "role": "human"},
        "to": {"agent_id": "agent-b", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Test Task",
            "acceptance_criteria": ["Tests pass"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "fix/test-task",
        },
        "policy": {
            "allowed_actions": [],
            "prohibited_actions": [],
            "human_approval_required": [],
        },
    }


def test_validate_task_endpoint_valid(client, valid_envelope):
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    data = response.json()
    assert data == {"valid": True, "errors": []}


def test_validate_task_endpoint_invalid(client):
    invalid_envelope = {
        "protocol_version": "1.0"
    }
    response = client.post("/v1/validate-task", json={"envelope": invalid_envelope})
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False


@pytest.mark.parametrize(
    ("field", "value", "expected_format"),
    [
        ("task_id", "private-invalid-task-id", "uuid"),
        ("message_id", "private-invalid-message-id", "uuid"),
        ("created_at", "2026-02-30T10:00:00Z", "date-time"),
        ("created_at", "2026-11-06T10:00:00", "date-time"),
    ],
)
def test_validate_task_rejects_invalid_formats(client, valid_envelope, field, value, expected_format):
    valid_envelope[field] = value
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    assert response.json() == {
        "valid": False,
        "errors": [f"/{field}: expected {expected_format} format"],
    }


def test_validate_task_redacts_extra_fields_and_reports_nested_errors(client, valid_envelope):
    valid_envelope["from"] = {"private-field-name": "private-value", "agent_id": "agent-a"}
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert set(response.json()["errors"]) == {
        "/from: missing required fields: role",
        "/from: remove unsupported fields",
    }


@pytest.mark.parametrize(
    "schema_text",
    [
        None,
        "{",
        '{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "invalid"}',
        '{"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}',
    ],
    ids=["missing", "malformed-json", "invalid-schema", "wrong-draft"],
)
def test_schema_load_failure_prevents_startup(monkeypatch, tmp_path, schema_text):
    schema_path = tmp_path / "task-envelope.schema.json"
    if schema_text is not None:
        schema_path.write_text(schema_text, encoding="utf-8")
    monkeypatch.setattr(serving, "TASK_SCHEMA_PATH", schema_path)

    with pytest.raises(RuntimeError, match="Unable to load a valid A2A task envelope schema"):
        with TestClient(serving.app):
            pytest.fail("Startup unexpectedly succeeded")


def test_validate_task_reuses_schema_loaded_at_startup(monkeypatch, tmp_path, valid_envelope):
    schema_path = tmp_path / "task-envelope.schema.json"
    schema_path.write_bytes(serving.TASK_SCHEMA_PATH.read_bytes())
    monkeypatch.setattr(serving, "TASK_SCHEMA_PATH", schema_path)

    with TestClient(serving.app) as client:
        schema_path.unlink()
        response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
        assert response.status_code == 200
        assert response.json() == {"valid": True, "errors": []}
