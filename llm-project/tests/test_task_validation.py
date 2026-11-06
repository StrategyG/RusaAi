import importlib
import json

import pytest
from fastapi.testclient import TestClient

serve = importlib.import_module("llm-project.serve.app")


@pytest.fixture
def valid_envelope():
    return {
        "protocol_version": "1.0",
        "task_id": "123e4567-e89b-12d3-a456-426614174000",
        "message_id": "123e4567-e89b-12d3-a456-426614174001",
        "type": "task.create",
        "from": {"agent_id": "client", "role": "human"},
        "to": {"agent_id": "orchestrator", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T12:00:00Z",
        "task": {
            "objective": "Validate the task envelope",
            "acceptance_criteria": ["The envelope is accepted"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "fix/schema-validation",
        },
        "policy": {
            "allowed_actions": ["read"],
            "prohibited_actions": ["deploy"],
            "human_approval_required": ["merge"],
        },
    }


def test_valid_envelope_is_accepted(valid_envelope):
    with TestClient(serve.app) as client:
        response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("task_id", "not-a-uuid", "task_id: has an invalid format"),
        ("created_at", "yesterday", "created_at: has an invalid format"),
    ],
)
def test_format_constraints_are_enforced(valid_envelope, field, value, expected):
    valid_envelope[field] = value
    with TestClient(serve.app) as client:
        response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert expected in response.json()["errors"]


def test_required_field_error_names_missing_field(valid_envelope):
    del valid_envelope["task_id"]
    with TestClient(serve.app) as client:
        response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.json()["errors"] == ["$.task_id: is required"]


def test_missing_schema_fails_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "SCHEMA_PATH", tmp_path / "missing.json")
    with pytest.raises(RuntimeError, match="Unable to load valid A2A"):
        with TestClient(serve.app):
            pass


def test_invalid_schema_fails_startup(monkeypatch, tmp_path):
    schema_path = tmp_path / "invalid.json"
    schema_path.write_text(json.dumps({"type": "not-a-json-schema-type"}))
    monkeypatch.setattr(serve, "SCHEMA_PATH", schema_path)
    with pytest.raises(RuntimeError, match="Unable to load valid A2A"):
        with TestClient(serve.app):
            pass
