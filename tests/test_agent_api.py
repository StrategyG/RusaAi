import json

from fastapi.testclient import TestClient

from rusa_agents import api


class FakeQueue:
    def __init__(self, dsn):
        self.tasks = {}
        self.idempotency = {}

    def check_ready(self):
        return True

    def enqueue(self, task):
        key = (task.tenant_id, task.idempotency_key or task.task_id)
        if key in self.idempotency:
            return self.idempotency[key]
        self.tasks[task.task_id] = {
            "tenant_id": task.tenant_id,
            "status": "queued",
            "result": None,
            "attempts": 0,
            "last_error": None,
        }
        self.idempotency[key] = task.task_id
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
    monkeypatch.setenv(
        "RUSA_AGENT_TENANT_REPOSITORIES",
        json.dumps({"tenant-a": ["StrategyG/RusaAi"], "tenant-b": ["StrategyG/RusaAi"]}),
    )
    monkeypatch.setattr(api, "PostgresTaskQueue", FakeQueue)


def task_body():
    return {
        "objective": "Add regression tests",
        "repository": "StrategyG/RusaAi",
        "base_branch": "main",
        "acceptance_criteria": ["Tests pass"],
    }


def test_agent_api_requires_authentication(monkeypatch):
    configure_api(monkeypatch)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/agent/tasks",
            headers={"X-Tenant-ID": "tenant-a"},
            json=task_body(),
        )
    assert response.status_code == 401


def test_agent_api_submits_and_reads_only_own_tenant_task(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    with TestClient(api.app) as client:
        created = client.post("/v1/agent/tasks", headers=headers, json=task_body())
        assert created.status_code == 202
        assert created.headers["X-Request-ID"]
        assert created.headers["Cache-Control"] == "no-store"
        assert created.json()["work_branch"].startswith("rusa-agent/")
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
    body = task_body()
    body["work_branch"] = "main"
    with TestClient(api.app) as client:
        response = client.post("/v1/agent/tasks", headers=headers, json=body)
    assert response.status_code == 422


def test_agent_api_rejects_unmapped_tenant_key(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-c"}
    with TestClient(api.app) as client:
        response = client.get("/v1/agent/tasks/missing", headers=headers)
    assert response.status_code == 401


def test_agent_api_rejects_duplicate_tenant_keys(monkeypatch):
    monkeypatch.setenv(
        "RUSA_AGENT_TENANT_KEYS",
        json.dumps({"tenant-a": "same-key-" + "x" * 40, "tenant-b": "same-key-" + "x" * 40}),
    )
    monkeypatch.setenv("RUSA_AGENT_API_DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(api, "PostgresTaskQueue", FakeQueue)
    with TestClient(api.app) as client:
        response = client.get(
            "/v1/agent/tasks/missing",
            headers={"X-API-Key": "same-key-" + "x" * 40, "X-Tenant-ID": "tenant-a"},
        )
    assert response.status_code == 503


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


def test_agent_api_reuses_same_tenant_branch_for_idempotent_retry(monkeypatch):
    configure_api(monkeypatch)
    headers = {
        "X-API-Key": "a" * 48,
        "X-Tenant-ID": "tenant-a",
        "Idempotency-Key": "retry-123",
    }
    with TestClient(api.app) as client:
        first = client.post("/v1/agent/tasks", headers=headers, json=task_body())
        second = client.post("/v1/agent/tasks", headers=headers, json=task_body())
    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert first.json()["work_branch"] == second.json()["work_branch"]



def test_agent_api_rejects_repository_outside_tenant_allowlist(monkeypatch):
    configure_api(monkeypatch)
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    body = task_body()
    body["repository"] = "another-owner/private-repo"
    with TestClient(api.app) as client:
        response = client.post("/v1/agent/tasks", headers=headers, json=body)
    assert response.status_code == 403


def test_agent_api_enforces_streamed_body_limit_without_content_length():
    import asyncio

    from rusa_agents.api import RequestGuardMiddleware

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestGuardMiddleware(downstream, max_request_bytes=4)
    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/test", "headers": [], "state": {}}
    asyncio.run(middleware(scope, receive, send))
    response_start = next(item for item in sent if item["type"] == "http.response.start")
    assert response_start["status"] == 413


def test_agent_api_times_out_slow_request_body():
    import asyncio

    from rusa_agents.api import RequestGuardMiddleware

    sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    async def downstream(*args):
        raise AssertionError("incomplete request must not reach downstream app")

    middleware = RequestGuardMiddleware(
        downstream, max_request_bytes=8, body_timeout_seconds=0.01,
    )
    scope = {
        "type": "http", "method": "POST", "path": "/test",
        "headers": [], "state": {},
    }
    asyncio.run(middleware(scope, receive, send))
    response_start = next(
        item for item in sent if item["type"] == "http.response.start"
    )
    assert response_start["status"] == 408
