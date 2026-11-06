import hashlib
import json
import uuid
from types import SimpleNamespace

from app import app, set_inference_provider
from fastapi.testclient import TestClient
from rusa_agents.providers import InferenceStreamEvent

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

        def enqueue(self, task_id, tenant_id, payload, *, audit_actor=None):
            self.items[(tenant_id, task_id)] = payload
            return True

        def get(self, task_id, tenant_id, *, audit_actor=None):
            payload = self.items.get((tenant_id, task_id))
            if payload is None:
                return None
            return {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "payload": payload,
                "result": None,
            }

        def retry(self, task_id, tenant_id, *, audit_actor=None):
            return (tenant_id, task_id) in self.items

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
    assert data["work_branch_prefix"].startswith("feature/aicode/")
    assert queue.get(data["task_id"], "tenant-alpha") is not None
    assert queue.get(data["task_id"], "tenant-beta") is None


def test_aicode_task_rejects_unauthorized_repository(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    production_key = "tenant-key-production-test-credential-000000"
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        '{"' + hashlib.sha256(production_key.encode()).hexdigest()
        + '":{"subject":"tenant-admin","tenant_id":"tenant-alpha","roles":["tenant_admin"]}}',
    )
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-alpha":["StrategyG/Allowed"]}')

    class AuditSink:
        def record(self, tenant_id, task_id, actor, action, decision):
            return None

    monkeypatch.setattr(app.state, "task_policy_audit", AuditSink(), raising=False)
    monkeypatch.setattr(app.state, "queue", object(), raising=False)
    response = client.post(
        "/v1/aicode/tasks",
        headers={"X-API-Key": production_key},
        json={
            "objective": "Try another repository",
            "repository": {"full_name": "someone/private-repo"},
            "acceptance_criteria": ["Must be denied"],
        },
    )
    assert response.status_code == 403


def test_metrics_endpoint_requires_operator_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-metrics"}')
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "operator-metrics-key")
    denied = client.get("/metrics")
    assert denied.status_code == 401
    tenant_denied = client.get("/metrics", headers={"X-API-Key": "tenant-key"})
    assert tenant_denied.status_code == 401
    response = client.get("/metrics", headers={"X-API-Key": "operator-metrics-key"})
    assert response.status_code == 200
    assert "rusa_api_requests_total" in response.text



def test_aicode_task_retry_is_tenant_scoped(monkeypatch):
    class RetryQueue:
        def retry(self, task_id, tenant_id, *, audit_actor=None):
            return tenant_id == "tenant-alpha" and task_id == "00000000-0000-4000-8000-000000000001"

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setattr(app.state, "queue", RetryQueue(), raising=False)
    task_id = "00000000-0000-4000-8000-000000000001"
    accepted = client.post(
        f"/v1/aicode/tasks/{task_id}/retry",
        headers={"X-API-Key": "tenant-key"},
    )
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"

    invalid = client.post(
        "/v1/aicode/tasks/not-a-uuid/retry",
        headers={"X-API-Key": "tenant-key"},
    )
    assert invalid.status_code == 422



def test_readiness_checks_inference_and_database(monkeypatch):
    class ReadyProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            return "ok"

    class ReadyQueue:
        def ping(self):
            return True

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setattr(app.state, "queue", ReadyQueue(), raising=False)
    set_inference_provider(ReadyProvider())
    try:
        response = client.get("/health/ready")
    finally:
        set_inference_provider(None)

    assert response.status_code == 200
    assert response.json()["inference"] == "reachable"
    assert response.json()["database"] == "reachable"


def test_readiness_fails_when_database_is_unavailable(monkeypatch):
    class ReadyProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            return "ok"

    class BrokenQueue:
        def ping(self):
            raise RuntimeError("database unavailable")

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setattr(app.state, "queue", BrokenQueue(), raising=False)
    set_inference_provider(ReadyProvider())
    try:
        response = client.get("/health/ready")
    finally:
        set_inference_provider(None)

    assert response.status_code == 503
    assert response.json()["detail"] == "Task database is unavailable"


def test_aicode_task_intake_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "false")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setattr(app.state, "queue", object(), raising=False)
    response = client.post(
        "/v1/aicode/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "objective": "Add a test",
            "repository": {"full_name": "StrategyG/RusaAi"},
            "acceptance_criteria": ["The test passes"],
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "AICode task intake is disabled"


def test_aicode_retry_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "off")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setattr(app.state, "queue", object(), raising=False)
    response = client.post(
        "/v1/aicode/tasks/00000000-0000-4000-8000-000000000001/retry",
        headers={"X-API-Key": "tenant-key"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "AICode task intake is disabled"



def test_aicode_task_status_exposes_result_only_to_owning_tenant(monkeypatch):
    task_id = "00000000-0000-4000-8000-000000000001"

    class ResultQueue:
        def get(self, requested_id, tenant_id, *, audit_actor=None):
            if requested_id != task_id or tenant_id != "tenant-alpha":
                return None
            return {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "status": "succeeded",
                "result": {
                    "pull_request_url": "https://github.com/StrategyG/RusaAi/pull/99",
                    "commit_sha": "a" * 40,
                    "branch": "feature/aicode/000000000001-deadbeef",
                },
            }

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setattr(app.state, "queue", ResultQueue(), raising=False)

    own = client.get(
        f"/v1/aicode/tasks/{task_id}",
        headers={"X-API-Key": "tenant-key"},
    )
    assert own.status_code == 200
    assert own.json()["result"]["pull_request_url"].endswith("/pull/99")
    assert own.json()["result"]["commit_sha"] == "a" * 40

    foreign = client.get(
        f"/v1/aicode/tasks/{task_id}",
        headers={"X-API-Key": "foreign-key"},
    )
    assert foreign.status_code == 401


def test_task_rbac_denies_read_only_principal_and_audits_decision(monkeypatch, caplog):
    key = "read-only-task-test-credential-000000000000"
    digest = hashlib.sha256(key.encode()).hexdigest()
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        '{"' + digest + '":{"subject":"reader-service","tenant_id":"tenant-reader","roles":["read_only"]}}',
    )
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    monkeypatch.setattr(app.state, "queue", object(), raising=False)
    caplog.set_level("INFO", logger="rusa-ai.task-policy")

    response = client.post(
        "/v1/aicode/tasks",
        headers={"X-API-Key": key},
        json={
            "objective": "This must be denied",
            "repository": {"full_name": "StrategyG/RusaAi"},
            "acceptance_criteria": ["Must not enqueue"],
        },
    )

    assert response.status_code == 403
    events = [
        __import__("json").loads(record.getMessage())
        for record in caplog.records
        if record.name == "rusa-ai.task-policy"
    ]
    assert events[-1]["tenant_id"] == "tenant-reader"
    assert events[-1]["task_id"] is None
    assert events[-1]["actor"] == "api:reader-service"
    assert events[-1]["action"] == "task.create"
    assert events[-1]["decision"] == "deny"

def test_chat_completions_streams_openai_sse_and_usage(monkeypatch):
    class StreamingProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            return "unused"

        async def stream(self, messages, **parameters):
            yield InferenceStreamEvent(content="hello ")
            yield InferenceStreamEvent(content="world")
            yield InferenceStreamEvent(
                finish_reason="stop",
                prompt_tokens=3,
                completion_tokens=2,
            )

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        SimpleNamespace(environment="development", model_name="rusa-ai"),
        raising=False,
    )
    set_inference_provider(StreamingProvider())
    try:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "rusa-ai",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert '"content":"hello "' in response.text
    assert '"content":"world"' in response.text
    assert '"finish_reason":"stop"' in response.text
    assert '"total_tokens":5' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_inference_denies_read_only_tenant_role(monkeypatch):
    class StubProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            return "must not be called"

    key = "read-only-inference-test-credential-000000"
    digest = hashlib.sha256(key.encode()).hexdigest()
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        json.dumps({
            digest: {
                "subject": "reader-service",
                "tenant_id": "tenant-reader",
                "roles": ["read_only"],
            }
        }),
    )
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    set_inference_provider(StubProvider())
    try:
        response = client.post(
            "/v1/chat/completions",
            headers={"X-API-Key": key},
            json={
                "model": "rusa-ai",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 403
    assert response.json()["detail"] == "Insufficient inference permissions"


def test_inference_rejects_unconfigured_model(monkeypatch):
    class StubProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            raise AssertionError("provider must not receive an unconfigured model")

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        SimpleNamespace(environment="development", model_name="rusa-ai"),
        raising=False,
    )
    set_inference_provider(StubProvider())
    try:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "unapproved-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 404

