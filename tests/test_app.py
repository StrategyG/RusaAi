import os
import sys
import uuid
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")))

from app import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_validate_task_endpoint_valid():
    valid_envelope = {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": {"agent_id": "agent-a", "role": "chatgpt_app"},
        "to": {"agent_id": "agent-b", "role": "github_copilot"},
        "created_at": "2026-11-06T10:00:00Z",
        "task": {
            "objective": "Test Task",
            "acceptance_criteria": ["Complete the test"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "feature/test",
        },
        "policy": {"allowed_actions": [], "prohibited_actions": [], "human_approval_required": []}
    }
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True


def test_validate_task_endpoint_invalid():
    invalid_envelope = {
        "protocol_version": "1.0"
    }
    response = client.post("/v1/validate-task", json={"envelope": invalid_envelope})
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
