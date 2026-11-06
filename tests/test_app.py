import os
import sys
import uuid

import jsonschema
import pytest
from fastapi.testclient import TestClient

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../llm-project/serve")
    ),
)

from app import app, load_task_validator  # noqa: E402

client = TestClient(app)


def valid_task_envelope():
    return {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "agent-a", "role": "chatgpt_app"},
        "to": {"agent_id": "agent-b", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Run the smoke test",
            "acceptance_criteria": ["The task is validated"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "fix/fixture-validation",
        },
        "policy": {
            "allowed_actions": ["read_repository"],
            "prohibited_actions": ["exfiltrate_secrets"],
            "human_approval_required": ["merge", "deploy"],
        },
    }


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_chat_completions_endpoint():
    payload = {"messages": [{"role": "user", "content": "Hello"}]}
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    assert "choices" in response.json()


def test_validate_task_endpoint_valid():
    response = client.post(
        "/v1/validate-task", json={"envelope": valid_task_envelope()}
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_validate_task_endpoint_invalid():
    response = client.post(
        "/v1/validate-task", json={"envelope": {"protocol_version": "1.0"}}
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_validate_task_rejects_invalid_uuid():
    envelope = valid_task_envelope()
    envelope["task_id"] = "not-a-uuid"
    response = client.post("/v1/validate-task", json={"envelope": envelope})
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_schema_validator_uses_draft_2020_12_and_format_checker():
    validator = load_task_validator()
    assert isinstance(validator, jsonschema.Draft202012Validator)
    assert validator.FORMAT_CHECKER is not None


def test_missing_schema_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="schema is missing or invalid"):
        load_task_validator(tmp_path / "missing.json")


def test_invalid_schema_fails_closed(tmp_path):
    schema_path = tmp_path / "invalid.json"
    schema_path.write_text('{"type": "not-a-json-schema-type"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="schema is missing or invalid"):
        load_task_validator(schema_path)
