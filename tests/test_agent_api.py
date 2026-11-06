import json

from fastapi.testclient import TestClient

from rusa_agents import api


class FakeQueue:
    def __init__(self, dsn):
        self.tasks = {}

    def check_ready(self):
        return True

    def enqueue(self, task):
        self.tasks[task.task_id] = {
            "tenant_id": task.tenant_id,
            "status": "queued",
            "result": None,
            "attempts": 0,
            "last_error": None,
        }
        return task.task_id

    def get(self, tenant_id, task_id):
        item = self.tasks.get(task_id)
        if item is None or item["tenant_id"] != tenant_id:
            return None
        return item


def configure_api(monkeypatch):
    monkeypatch.setenv(
        "RUSA_AGENT_TENANT_KEYS",
        json.dumps({"tenant-a": "a" * 48, "tenant-b": "b" * 48}),
    )
    monkeypatch.setenv("RUSA_AGENT_API_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(api, "PostgresTaskQueue", FakeQueue)


def test_agent_api_requires_authentication(monkeypatch):
    configure_api(monkeypatch)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/agent/tasks",
            headers={"X-Tenant-ID": "tenant-a"},
            json={},
        )
    assert response.status_code == 401


def test_agent_api_submits_and_reads_only_own_tenant_task(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = {
        "objective": "Add regression tests",
        "repository": "StrategyG/RusaAi",
        "base_branch": "main",
        "work_branch": "feature/api-test",
        "acceptance_criteria": ["Tests pass"],
    }
    with TestClient(api.app) as client:
        created = client.post("/v1/agent/tasks", headers=headers, json=body)
        assert created.status_code == 202
        assert created.headers["X-Request-ID"]
        assert created.headers["Cache-Control"] == "no-store"
        task_id = created.json()["task_id"]
        fetched = client.get(f"/v1/agent/tasks/{task_id}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "queued"
        other_tenant = client.get(
            f"/v1/agent/tasks/{task_id}",
            headers={"X-API-Key": "b" * 48, "X-Tenant-ID": "tenant-b"},
        )
    assert other_tenant.status_code == 404


def test_agent_api_rejects_invalid_branch_target(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = {
        "objective": "Change repository",
        "repository": "StrategyG/RusaAi",
        "base_branch": "main",
        "work_branch": "main",
        "acceptance_criteria": ["No regression"],
    }
    with TestClient(api.app) as client:
        response = client.post("/v1/agent/tasks", headers=headers, json=body)
    assert response.status_code == 422


def test_agent_api_rejects_unmapped_tenant_key(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-c"}
    with TestClient(api.app) as client:
        response = client.get("/v1/agent/tasks/missing", headers=headers)
    assert response.status_code == 401


def test_agent_api_enforces_request_size_limit(monkeypatch):
    configure_api(monkeypatch)
    monkeypatch.setattr(api, "MAX_AGENT_REQUEST_BYTES", 16)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/agent/tasks",
            headers=headers,
            json={"objective": "A" * 100},
        )
    assert response.status_code == 413
