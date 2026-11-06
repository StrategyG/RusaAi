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


def validate(envelope):
    with TestClient(serve.app) as client:
        return client.post("/v1/validate-task", json={"envelope": envelope})


def test_valid_envelope_is_accepted(valid_envelope):
    response = validate(valid_envelope)

    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "not-a-uuid"),
        ("created_at", "yesterday"),
    ],
)
def test_uuid_and_date_time_formats_are_enforced(valid_envelope, field, value):
    valid_envelope[field] = value

    response = validate(valid_envelope)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any("invalid" not in error and "format" in error for error in response.json()["errors"])


def test_uri_format_is_enforced(valid_envelope):
    valid_envelope["evidence"] = [{"kind": "commit", "uri": "not a uri"}]

    response = validate(valid_envelope)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert "$.evidence[0].uri: must use the uri format" in response.json()["errors"]


def test_valid_uri_is_accepted(valid_envelope):
    valid_envelope["evidence"] = [
        {"kind": "commit", "uri": "https://github.com/StrategyG/RusaAi/commit/abc1234"}
    ]

    response = validate(valid_envelope)

    assert response.status_code == 200
    assert response.json() == {"valid": True, "errors": []}


def test_validation_error_response_is_bounded(valid_envelope):
    valid_envelope["task"]["acceptance_criteria"] = [""] * 60

    response = validate(valid_envelope)

    errors = response.json()["errors"]
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert len(errors) == 51
    assert errors[-1].startswith("$: more than 50 validation errors")


def test_required_errors_identify_every_missing_field(valid_envelope):
    del valid_envelope["task_id"]
    del valid_envelope["message_id"]

    response = validate(valid_envelope)

    assert response.status_code == 200
    assert set(response.json()["errors"]) == {
        "$.message_id: is required",
        "$.task_id: is required",
    }


def test_nested_required_error_uses_json_path(valid_envelope):
    del valid_envelope["task"]["objective"]

    response = validate(valid_envelope)

    assert "$.task.objective: is required" in response.json()["errors"]


def test_errors_do_not_echo_submitted_values(valid_envelope):
    valid_envelope["unexpected"] = "do-not-return-this-secret"

    response = validate(valid_envelope)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert "do-not-return-this-secret" not in response.text
    assert "$: contains an unsupported field" in response.json()["errors"]


def test_missing_schema_fails_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "SCHEMA_PATH", tmp_path / "missing.json")

    with pytest.raises(RuntimeError, match="Unable to load valid A2A"):
        with TestClient(serve.app):
            pass


@pytest.mark.parametrize(
    "schema_text",
    [
        "{invalid-json",
        json.dumps({"type": "not-a-json-schema-type"}),
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2019-09/schema",
                "type": "object",
            }
        ),
    ],
)
def test_corrupt_or_wrong_draft_schema_fails_startup(
    monkeypatch, tmp_path, schema_text
):
    schema_path = tmp_path / "invalid.json"
    schema_path.write_text(schema_text, encoding="utf-8")
    monkeypatch.setattr(serve, "SCHEMA_PATH", schema_path)

    with pytest.raises(RuntimeError, match="Unable to load valid A2A"):
        with TestClient(serve.app):
            pass
