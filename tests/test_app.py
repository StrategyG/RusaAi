import uuid

from fastapi.testclient import TestClient

from app import app, set_inference_provider


client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_chat_completions_fails_closed_without_backend():
    set_inference_provider(None)
    payload = {"messages": [{"role": "user", "content": "Hello"}]}
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 503
    assert response.json()["detail"] == "Inference backend is not configured"


def test_chat_completions_uses_injected_inference_provider():
    class StubInferenceProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            return "real provider response"

    set_inference_provider(StubInferenceProvider())
    try:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "real provider response"
    finally:
        set_inference_provider(None)


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
            "work_branch": "feature/test-task",
        },
        "policy": {
            "allowed_actions": ["repo_read"],
            "prohibited_actions": ["deploy"],
            "human_approval_required": ["merge", "deploy"],
        },
    }
    response = client.post("/v1/validate-task", json={"envelope": valid_envelope})
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_validate_task_endpoint_invalid():
    response = client.post("/v1/validate-task", json={"envelope": {"protocol_version": "1.0"}})
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_aicode_task_intake_is_tenant_scoped(monkeypatch):
    class StubQueue:
        def __init__(self):
            self.items = {}

        def enqueue(self, task_id, tenant_id, payload):
            self.items[(tenant_id, task_id)] = payload
            return True

        def get(self, task_id, tenant_id):
            payload = self.items.get((tenant_id, task_id))
            if payload is None:
                return None
            return {"task_id": task_id, "tenant_id": tenant_id, "payload": payload}

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-alpha":["StrategyG/RusaAi"]}')
    queue = StubQueue()
    monkeypatch.setattr(app.state, "queue", queue, raising=False)

    response = client.post(
        "/v1/aicode/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "objective": "Add a health endpoint test",
            "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
            "acceptance_criteria": ["The test passes"],
        },
    )
    assert response.status_code == 202
    data = response.json()
    assert data["tenant_id"] == "tenant-alpha"
    assert data["work_branch"].startswith("feature/aicode/")
    assert queue.get(data["task_id"], "tenant-alpha") is not None
    assert queue.get(data["task_id"], "tenant-beta") is None


def test_aicode_task_rejects_unauthorized_repository(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-alpha":["StrategyG/Allowed"]}')
    monkeypatch.setattr(app.state, "queue", object(), raising=False)
    response = client.post(
        "/v1/aicode/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "objective": "Try another repository",
            "repository": {"full_name": "someone/private-repo"},
            "acceptance_criteria": ["Must be denied"],
        },
    )
    assert response.status_code == 403


def test_metrics_endpoint_requires_authenticated_dependency_and_returns_metrics():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "rusa_api_requests_total" in response.text
