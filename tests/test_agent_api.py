import json

from fastapi.testclient import TestClient

from rusa_agents import api
from rusa_agents.queue import IdempotencyConflictError


class FakeQueue:
    def __init__(self, dsn):
        self.tasks = {}
        self.idempotency = {}
        self.idempotency_payloads = {}

    def check_ready(self):
        return True

    def enqueue(self, task):
        key = (task.tenant_id, task.idempotency_key or task.task_id)
        payload = task.to_dict()
        if key in self.idempotency:
            existing = dict(self.idempotency_payloads[key])
            existing.pop("task_id", None)
            existing.pop("created_at", None)
            candidate = dict(payload)
            candidate.pop("task_id", None)
            candidate.pop("created_at", None)
            if existing != candidate:
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different task payload"
                )
            return self.idempotency[key]
        self.tasks[task.task_id] = {
            "tenant_id": task.tenant_id,
            "status": "queued",
            "result": None,
            "attempts": 0,
            "last_error": None,
        }
        self.idempotency[key] = task.task_id
        self.idempotency_payloads[key] = payload
        return task.task_id

    def get(self, tenant_id, task_id):
        item = self.tasks.get(task_id)
        if item is None or item["tenant_id"] != tenant_id:
            return None
        return item

    def cancel(self, tenant_id, task_id):
        item = self.get(tenant_id, task_id)
        if item is None or item["status"] not in {
            "queued", "retry", "waiting_external", "running"
        }:
            return False
        item["status"] = "cancelled"
        return True

    def requeue_failed(self, tenant_id, task_id):
        item = self.get(tenant_id, task_id)
        if item is None or item["status"] != "failed":
            return False
        item.update(status="queued", attempts=0, result=None, last_error=None)
        return True



def configure_api(monkeypatch):
    monkeypatch.setenv(
        "RUSA_AGENT_TENANT_KEYS",
        json.dumps({"tenant-a": "a" * 48, "tenant-b": "b" * 48}),
    )
    monkeypatch.setenv("RUSA_AGENT_API_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("RUSA_AGENT_ALLOWED_REPOSITORIES", "StrategyG/RusaAi,other-owner/other-repo")
    monkeypatch.setenv("RUSA_AGENT_TENANT_REPOSITORIES", json.dumps({
        "tenant-a": ["StrategyG/RusaAi"],
        "tenant-b": ["other-owner/other-repo"],
    }))
    monkeypatch.setattr(api, "PostgresTaskQueue", FakeQueue)


def test_agent_api_requires_authentication(monkeypatch):
    configure_api(monkeypatch)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/agent/tasks",
            headers={"X-Tenant-ID": "tenant-a"},
            json={
                "objective": "Authenticated task submission",
                "repository": "StrategyG/RusaAi",
                "acceptance_criteria": ["Tests pass"],
            },
        )
    assert response.status_code == 401


def test_agent_api_submits_and_reads_only_own_tenant_task(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = {
        "objective": "Add regression tests",
        "repository": "StrategyG/RusaAi",
        "base_branch": "main",
        "acceptance_criteria": ["Tests pass"],
    }
    with TestClient(api.app) as client:
        created = client.post("/v1/agent/tasks", headers=headers, json=body)
        assert created.status_code == 202
        assert created.headers["X-Request-ID"]
        assert created.headers["Cache-Control"] == "no-store"
        task_id = created.json()["task_id"]
        assert created.json()["work_branch"].startswith("rusa-agent/")
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


def test_agent_api_rejects_repository_outside_allowlist(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = {
        "objective": "Inspect another repository",
        "repository": "some-owner/private-repo",
        "acceptance_criteria": ["No changes"],
    }
    with TestClient(api.app) as client:
        response = client.post("/v1/agent/tasks", headers=headers, json=body)
    assert response.status_code == 403


def test_agent_api_idempotency_reuses_task_id(monkeypatch):
    configure_api(monkeypatch)
    headers = {
        "X-API-Key": "a" * 48,
        "X-Tenant-ID": "tenant-a",
        "Idempotency-Key": "same-request-key",
    }
    body = {
        "objective": "Add regression tests",
        "repository": "StrategyG/RusaAi",
        "acceptance_criteria": ["Tests pass"],
    }
    with TestClient(api.app) as client:
        first = client.post("/v1/agent/tasks", headers=headers, json=body)
        second = client.post("/v1/agent/tasks", headers=headers, json=body)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert first.json()["work_branch"] == second.json()["work_branch"]


def test_agent_api_rejects_idempotency_key_reuse_with_changed_payload(monkeypatch):
    configure_api(monkeypatch)
    headers = {
        "X-API-Key": "a" * 48,
        "X-Tenant-ID": "tenant-a",
        "Idempotency-Key": "same-request-key",
    }
    body = {
        "objective": "Add regression tests",
        "repository": "StrategyG/RusaAi",
        "acceptance_criteria": ["Tests pass"],
    }
    changed_body = dict(body, objective="Different task objective")
    with TestClient(api.app) as client:
        first = client.post("/v1/agent/tasks", headers=headers, json=body)
        second = client.post("/v1/agent/tasks", headers=headers, json=changed_body)
    assert first.status_code == 202
    assert second.status_code == 409
    assert "different task payload" in second.json()["detail"]


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


def test_agent_api_rejects_repository_assigned_to_another_tenant(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "b" * 48, "X-Tenant-ID": "tenant-b"}
    body = {
        "objective": "Inspect a globally allowed but unassigned repository",
        "repository": "StrategyG/RusaAi",
        "acceptance_criteria": ["No changes"],
    }
    with TestClient(api.app) as client:
        response = client.post("/v1/agent/tasks", headers=headers, json=body)
    assert response.status_code == 403


def test_agent_api_maps_queue_outage_to_retryable_503(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    with TestClient(api.app) as client:
        client.app.state.queue.get = lambda tenant_id, task_id: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        )
        response = client.get("/v1/agent/tasks/missing", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "Agent task queue is temporarily unavailable"



def test_agent_api_cancels_task_and_is_idempotent_for_own_tenant(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = {
        "objective": "Cancel this task",
        "repository": "StrategyG/RusaAi",
        "acceptance_criteria": ["Cancellation is tenant scoped"],
    }
    with TestClient(api.app) as client:
        created = client.post("/v1/agent/tasks", headers=headers, json=body)
        task_id = created.json()["task_id"]
        cancelled = client.delete(f"/v1/agent/tasks/{task_id}", headers=headers)
        repeated = client.delete(f"/v1/agent/tasks/{task_id}", headers=headers)
        other_tenant = client.delete(
            f"/v1/agent/tasks/{task_id}",
            headers={"X-API-Key": "b" * 48, "X-Tenant-ID": "tenant-b"},
        )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert repeated.status_code == 200
    assert other_tenant.status_code == 404


def test_agent_api_retries_only_failed_tasks(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = {
        "objective": "Retry a failed task",
        "repository": "StrategyG/RusaAi",
        "acceptance_criteria": ["Retry remains tenant scoped"],
    }
    with TestClient(api.app) as client:
        created = client.post("/v1/agent/tasks", headers=headers, json=body)
        task_id = created.json()["task_id"]
        rejected = client.post(f"/v1/agent/tasks/{task_id}/retry", headers=headers)
        client.app.state.queue.tasks[task_id]["status"] = "failed"
        retried = client.post(f"/v1/agent/tasks/{task_id}/retry", headers=headers)
        fetched = client.get(f"/v1/agent/tasks/{task_id}", headers=headers)
    assert rejected.status_code == 409
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    assert fetched.json()["status"] == "queued"


def test_agent_api_does_not_cancel_terminal_task(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = {
        "objective": "Terminal task",
        "repository": "StrategyG/RusaAi",
        "acceptance_criteria": ["Terminal states cannot be cancelled"],
    }
    with TestClient(api.app) as client:
        created = client.post("/v1/agent/tasks", headers=headers, json=body)
        task_id = created.json()["task_id"]
        client.app.state.queue.tasks[task_id]["status"] = "succeeded"
        response = client.delete(f"/v1/agent/tasks/{task_id}", headers=headers)
    assert response.status_code == 409
