import os
import sys
import uuid
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")))

from app import app

client = TestClient(app)
os.environ["RUSA_API_KEY"] = "test-key"
HEADERS = {"X-API-Key": "test-key"}


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_chat_completions_endpoint():
    payload = {
        "messages": [
            {"role": "user", "content": "Hello"}
        ]
    }
    response = client.post("/v1/chat/completions", json=payload, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data


def test_validate_task_endpoint_valid():
    valid_envelope = {
        "protocol_version": "1.0",
        "task_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "type": "task.create",
        "from": "agent-a",
        "to": "agent-b",
        "created_at": "2026-11-06T10:00:00Z",
        "task": {"name": "Test Task"},
        "policy": {}
    }
    response = client.post("/v1/validate-task", json={"task_envelope": valid_envelope}, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True


def test_validate_task_endpoint_invalid():
    invalid_envelope = {
        "protocol_version": "1.0"
    }
    response = client.post("/v1/validate-task", json={"task_envelope": invalid_envelope}, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
path.a
