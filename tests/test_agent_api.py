import json

from fastapi.testclient import TestClient

from rusa_agents import api


class FakeQueue:
    def __init__(self, dsn):
        self.tasks = {}
        self.idempotency = {}
        self.submitted = {}

    def check_ready(self):
        return True

    def enqueue(self, task):
        key = (task.tenant_id, task.idempotency_key or task.task_id)
        if key in self.idempotency:
            return self.idempotency[key]
        self.submitted[task.task_id] = task
        self.idempotency[key] = task.task_id
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
        "work_branch": "feature/api-test",
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



def test_agent_api_idempotency_reuses_task_and_branch(monkeypatch):
    configure_api(monkeypatch)
    headers = {
        "X-API-Key": "a" * 48,
        "X-Tenant-ID": "tenant-a",
        "Idempotency-Key": "same-task-request-001",
    }
    body = task_body()
    with TestClient(api.app) as client:
        first = client.post("/v1/agent/tasks", headers=headers, json=body)
        second = client.post("/v1/agent/tasks", headers=headers, json=body)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert first.json()["work_branch"] == second.json()["work_branch"]


def test_agent_api_assigns_unique_work_branches_per_task(monkeypatch):
    configure_api(monkeypatch)
    body = task_body()
    headers = {"X-API-Key": "a" * 48, "X-Tenant-ID": "tenant-a"}
    with TestClient(api.app) as client:
        first = client.post(
            "/v1/agent/tasks",
            headers={**headers, "Idempotency-Key": "branch-task-001"},
            json=body,
        )
        second = client.post(
            "/v1/agent/tasks",
            headers={**headers, "Idempotency-Key": "branch-task-002"},
            json=body,
        )
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["work_branch"] != second.json()["work_branch"]


def test_agent_api_times_out_slow_body_intake():
    import asyncio

    async def downstream(_scope, _receive, _send):
        raise AssertionError("downstream must not receive a timed-out body")

    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def run():
        sent = []
        async def send(message):
            sent.append(message)

        middleware = api.RequestGuardMiddleware(
            downstream,
            max_request_bytes=1024,
            body_timeout_seconds=0.001,
        )
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/agent/tasks",
            "headers": [],
            "state": {},
        }
        await middleware(scope, receive, send)
        return sent

    messages = asyncio.run(run())
    response_start = next(message for message in messages if message["type"] == "http.response.start")
    assert response_start["status"] == 408
