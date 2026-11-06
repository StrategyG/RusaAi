import os
import sys
import uuid
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")))

from app import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_chat_completions_endpoint():
    payload = {
        "messages": [
            {"role": "user", "content": "Hello"}
        ]
    }
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data


def test_validate_task_endpoint_valid():
    valid_envelope = {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "agent-a", "role": "human"},
        "to": {"agent_id": "agent-b", "role": "rusa_orchestrator"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Test A2A task validation",
            "acceptance_criteria": ["Tests pass"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "feature/test-a2a-task",
        },
        "policy": {
            "allowed_actions": ["repo_read"],
            "prohibited_actions": ["merge"],
            "human_approval_required": ["merge"],
        },
    }
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["errors"] == []


def test_validate_task_endpoint_invalid():
    invalid_envelope = {
        "protocol_version": "1.0"
    }
    response = client.post("/v1/validate-task", json={"envelope": invalid_envelope})
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
