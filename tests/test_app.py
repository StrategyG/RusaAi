import uuid

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_chat_completions_endpoint():
    payload = {"messages": [{"role": "user", "content": "Hello"}]}
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 503
    assert response.json()["detail"] == "Inference backend is not configured"


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
            "objective": "Test task",
            "acceptance_criteria": ["It validates"],
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "work_branch": "feature/test-task"
        },
        "policy": {
            "allowed_actions": ["repo_read"],
            "prohibited_actions": ["deploy"],
            "human_approval_required": ["merge", "deploy"]
        }
    }
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_validate_task_endpoint_invalid():
    response = client.post("/v1/validate-task", json={"envelope": {"protocol_version": "1.0"}})
    assert response.status_code == 200
    assert response.json()["valid"] is False
