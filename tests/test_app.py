import asyncio
import hashlib
import inspect
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from app import _await_with_deadline, app, set_inference_provider
from fastapi.testclient import TestClient

from rusa_agents.inference_admission import (
    InferenceAdmissionController,
    InferenceDeadlineTimeout,
)
from rusa_agents.providers import InferenceStreamEvent
from rusa_agents.runtime_v1 import AgentRuntimeV1

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def _configure_agent_runtime_api(monkeypatch, agent_runtime=None):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "agent_runtime",
        agent_runtime or AgentRuntimeV1(),
        raising=False,
    )


def test_agent_runtime_status_is_authenticated_and_reports_staged_registry(monkeypatch):
    _configure_agent_runtime_api(monkeypatch)
    response = client.get(
        "/v1/agent-runtime/status",
        headers={"X-API-Key": "tenant-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "draft_proposed_not_activated"
    assert data["role_count"] == 24
    assert data["dispatchable_roles"] == 0
    assert data["execution_enabled"] is False


def test_agent_runtime_route_preview_never_grants_tools_or_dispatches(monkeypatch):
    _configure_agent_runtime_api(monkeypatch)
    response = client.post(
        "/v1/agent-runtime/route-preview",
        headers={"X-API-Key": "tenant-key"},
        json={
            "scope": "worker",
            "required_capability_ids": ["RUSA-AW-WORKER-001"],
            "identity_type": "aicode_worker",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "PREVIEW_ONLY"
    assert data["selected_role_id"] is None
    assert data["suggested_role_id"] == "analyst"
    assert data["tool_grants"] == []
    assert data["candidates"][0]["dispatchable"] is False


def test_agent_runtime_route_preview_rejects_unknown_capability(monkeypatch):
    _configure_agent_runtime_api(monkeypatch)
    response = client.post(
        "/v1/agent-runtime/route-preview",
        headers={"X-API-Key": "tenant-key"},
        json={"scope": "worker", "required_capability_ids": ["UNKNOWN-CAPABILITY"]},
    )
    assert response.status_code == 422


def test_agent_runtime_plan_preview_validates_dag_and_uses_authenticated_tenant(monkeypatch):
    _configure_agent_runtime_api(monkeypatch)
    response = client.post(
        "/v1/agent-runtime/plan-preview",
        headers={"X-API-Key": "tenant-key"},
        json={
            "root_task_id": "plan-root-001",
            "objective": "Preview a small coding DAG",
            "nodes": [
                {
                    "task_id": "task-analyst",
                    "selected_role_id": "analyst",
                    "scope": "worker",
                    "objective": "Analyze the requested change",
                    "delegated_by_role_id": "supreme_orchestrator",
                    "acceptance_criteria": ["Return a scoped analysis"],
                },
                {
                    "task_id": "task-developer",
                    "selected_role_id": "developer",
                    "scope": "worker",
                    "objective": "Implement the approved change",
                    "delegated_by_role_id": "supreme_orchestrator",
                    "acceptance_criteria": ["Return a code change"],
                    "dependencies": ["task-analyst"],
                },
                {
                    "task_id": "task-tester",
                    "selected_role_id": "tester",
                    "scope": "worker",
                    "objective": "Verify the implementation",
                    "delegated_by_role_id": "supreme_orchestrator",
                    "acceptance_criteria": ["Return test evidence"],
                    "dependencies": ["task-developer"],
                    "risk_class": "high",
                },
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["tenant_id"] == "tenant-alpha"
    assert data["topological_order"] == [
        "task-analyst", "task-developer", "task-tester"
    ]
    assert data["approval_required_node_ids"] == ["task-tester"]
    assert data["registry_status"] == "draft_proposed_not_activated"
    assert data["execution_enabled"] is False
    assert data["execution_dispatched"] is False


def test_agent_runtime_plan_preview_rejects_caller_supplied_tenant(monkeypatch):
    _configure_agent_runtime_api(monkeypatch)
    response = client.post(
        "/v1/agent-runtime/plan-preview",
        headers={"X-API-Key": "tenant-key"},
        json={
            "root_task_id": "plan-root-tenant",
            "tenant_id": "tenant-attacker",
            "objective": "Attempt to override tenant context",
            "nodes": [],
        },
    )
    assert response.status_code == 422


def test_agent_runtime_task_intake_enqueues_tenant_bound_plan(monkeypatch):
    class ActiveRuntime:
        def __init__(self):
            self.validated_plan = None

        def status(self):
            return {"execution_enabled": True}

        def validate_plan(self, plan):
            self.validated_plan = plan
            return SimpleNamespace(topological_order=("task-analyst",))

    class Queue:
        def __init__(self):
            self.created = None

        def enqueue(self, task_id, tenant_id, payload, *, audit_actor=None):
            self.created = (task_id, tenant_id, payload, audit_actor)
            return True

    runtime = ActiveRuntime()
    _configure_agent_runtime_api(monkeypatch, runtime)
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    queue = Queue()
    monkeypatch.setattr(app.state, "queue", queue, raising=False)

    response = client.post(
        "/v1/agent-runtime/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "objective": "Execute a validated Agent Runtime DAG",
            "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
            "nodes": [{
                "task_id": "task-analyst",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the requested task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return verified analysis"],
            }],
        },
    )

    assert response.status_code == 202
    data = response.json()
    assert data["tenant_id"] == "tenant-alpha"
    assert data["execution_mode"] == "agent_runtime_v1"
    assert data["execution_enabled"] is True
    assert runtime.validated_plan.root_task_id == data["task_id"]
    assert runtime.validated_plan.tenant_id == "tenant-alpha"
    assert queue.created[0] == data["task_id"]
    assert queue.created[1] == "tenant-alpha"
    assert queue.created[2]["execution_mode"] == "agent_runtime_v1"
    assert queue.created[2]["repository"] == {"full_name": "StrategyG/RusaAi", "base_branch": "main"}
    assert "tenant_id" not in queue.created[2]["agent_runtime_plan"]
    assert queue.created[2]["agent_runtime_plan"]["nodes"][0]["task_id"] == "task-analyst"


def test_agent_runtime_task_intake_rejects_repository_outside_tenant_allowlist(monkeypatch):
    class ActiveRuntime:
        def status(self):
            return {"execution_enabled": True}

    class Queue:
        called = False

        def enqueue(self, *args, **kwargs):
            self.called = True
            return True

    _configure_agent_runtime_api(monkeypatch, ActiveRuntime())
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-alpha":["Other/Repo"]}')
    queue = Queue()
    monkeypatch.setattr(app.state, "queue", queue, raising=False)

    response = client.post(
        "/v1/agent-runtime/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "objective": "Reject a repository outside the tenant scope",
            "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
            "nodes": [{
                "task_id": "task-analyst",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the requested task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return verified analysis"],
            }],
        },
    )

    assert response.status_code == 403
    assert queue.called is False


def test_agent_runtime_task_intake_fails_closed_while_registry_is_draft(monkeypatch):
    class Queue:
        called = False

        def enqueue(self, *args, **kwargs):
            self.called = True
            return True

    _configure_agent_runtime_api(monkeypatch)
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    queue = Queue()
    monkeypatch.setattr(app.state, "queue", queue, raising=False)

    response = client.post(
        "/v1/agent-runtime/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "objective": "Do not enqueue before registry activation",
            "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
            "nodes": [{
                "task_id": "task-analyst",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the requested task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return verified analysis"],
            }],
        },
    )

    assert response.status_code == 409
    assert queue.called is False


def test_agent_runtime_task_intake_rejects_caller_tenant_override(monkeypatch):
    _configure_agent_runtime_api(monkeypatch)
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    response = client.post(
        "/v1/agent-runtime/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "tenant_id": "tenant-attacker",
            "objective": "Attempt to override authenticated tenant",
            "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
            "nodes": [{
                "task_id": "task-analyst",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the requested task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return verified analysis"],
            }],
        },
    )
    assert response.status_code == 422


def test_agent_runtime_task_intake_respects_kill_switch(monkeypatch):
    class ActiveRuntime:
        def status(self):
            return {"execution_enabled": True}

    class Queue:
        called = False

        def enqueue(self, *args, **kwargs):
            self.called = True
            return True

    _configure_agent_runtime_api(monkeypatch)
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "false")
    queue = Queue()
    monkeypatch.setattr(app.state, "queue", queue, raising=False)

    response = client.post(
        "/v1/agent-runtime/tasks",
        headers={"X-API-Key": "tenant-key"},
        json={
            "objective": "Respect the task intake kill switch",
            "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
            "nodes": [{
                "task_id": "task-analyst",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the requested task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return verified analysis"],
            }],
        },
    )
    assert response.status_code == 503
    assert queue.called is False


def test_agent_runtime_status_requires_task_read_permission(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    response = client.get("/v1/agent-runtime/status")
    assert response.status_code == 401


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


def test_chat_completions_validates_and_forwards_json_response_format(monkeypatch):
    captured = {}

    class StubInferenceProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            captured.update(parameters)
            return '{"done": true}'

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        SimpleNamespace(environment="development", model_name="rusaai-cpu-smoke-test"),
        raising=False,
    )
    set_inference_provider(StubInferenceProvider())
    try:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "rusaai-cpu-smoke-test",
                "messages": [{"role": "user", "content": "Return JSON"}],
                "response_format": {"type": "json_object"},
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 200
    assert captured["response_format"] == {"type": "json_object"}


def test_chat_completions_rejects_unsupported_response_format():
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "response_format": {"type": "text"},
        },
    )
    assert response.status_code == 422


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



def test_aicode_task_idempotency_header_replays_original_task(monkeypatch):
    class IdempotentQueue:
        def __init__(self):
            self.by_key = {}

        def enqueue_idempotent(
            self, task_id, tenant_id, payload, key, *, audit_actor=None
        ):
            existing = self.by_key.get((tenant_id, key))
            normalized = json.loads(json.dumps(payload))
            normalized["repository"].pop("work_branch_prefix", None)
            if existing:
                old_id, old_payload = existing
                if old_payload != normalized:
                    raise ValueError("Idempotency-Key was already used with a different task payload")
                return old_id, False, "queued"
            self.by_key[(tenant_id, key)] = (task_id, normalized)
            return task_id, True, "queued"

        def get(self, task_id, tenant_id, *, audit_actor=None):
            return {"task_id": task_id, "tenant_id": tenant_id, "status": "queued"}

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_AICODE_ENABLED", "true")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-alpha":["StrategyG/RusaAi"]}')
    queue = IdempotentQueue()
    monkeypatch.setattr(app.state, "queue", queue, raising=False)
    payload = {
        "objective": "Add a health endpoint test",
        "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
        "acceptance_criteria": ["The test passes"],
    }
    headers = {"X-API-Key": "tenant-key", "Idempotency-Key": "request-123"}
    first = client.post("/v1/aicode/tasks", headers=headers, json=payload)
    replay = client.post("/v1/aicode/tasks", headers=headers, json=payload)
    assert first.status_code == replay.status_code == 202
    assert first.json()["task_id"] == replay.json()["task_id"]
    assert replay.json()["work_branch_prefix"] == first.json()["work_branch_prefix"]

    changed = {**payload, "objective": "Do a different task"}
    conflict = client.post("/v1/aicode/tasks", headers=headers, json=changed)
    assert conflict.status_code == 409



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




def test_api_session_returns_tenant_bound_identity(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    response = client.get(
        "/v1/auth/session",
        headers={"X-API-Key": "tenant-key"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "subject": "legacy:tenant-alpha",
        "tenant_id": "tenant-alpha",
        "roles": ["tenant_admin"],
    }


def test_aicode_task_list_is_tenant_scoped_and_filterable(monkeypatch):
    class StubQueue:
        def list(
            self, tenant_id, *, status_filter=None, limit=50, offset=0, audit_actor=None
        ):
            assert tenant_id == "tenant-alpha"
            assert status_filter == "queued"
            assert audit_actor == "api:legacy:tenant-alpha"
            return {
                "items": [{
                    "task_id": "00000000-0000-4000-8000-000000000001",
                    "tenant_id": tenant_id,
                    "payload": {"objective": "Tenant-scoped task"},
                    "result": None,
                    "status": "queued",
                    "attempts": 0,
                    "max_attempts": 3,
                    "last_error": None,
                    "created_at": "2026-11-09T00:00:00+00:00",
                    "updated_at": "2026-11-09T00:00:00+00:00",
                }],
                "total": 1,
                "limit": limit,
                "offset": offset,
            }

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    monkeypatch.setattr(app.state, "queue", StubQueue(), raising=False)
    response = client.get(
        "/v1/aicode/tasks?status=queued&limit=10&offset=0",
        headers={"X-API-Key": "tenant-key"},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["tenant_id"] == "tenant-alpha"


def test_aicode_task_list_rejects_unknown_status(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_API_KEYS", '{"tenant-key":"tenant-alpha"}')
    response = client.get(
        "/v1/aicode/tasks?status=deploying",
        headers={"X-API-Key": "tenant-key"},
    )
    assert response.status_code == 422



def test_readiness_fails_closed_when_provider_reports_not_ready():
    class NotReadyInferenceProvider:
        async def check_ready(self):
            return False

        async def complete(self, messages, **parameters):
            raise AssertionError("A not-ready provider must not generate")

    set_inference_provider(NotReadyInferenceProvider())
    try:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["detail"] == "Inference backend is not ready"
    finally:
        set_inference_provider(None)



def test_chat_completions_enforces_generation_deadline_and_releases_slot(monkeypatch):
    class SlowProvider:
        cancelled = False

        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return "too late"

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        SimpleNamespace(environment="development", model_name="rusaai-cpu-smoke-test"),
        raising=False,
    )
    controller = InferenceAdmissionController(max_active=1, max_waiting=1, queue_timeout=1)
    monkeypatch.setattr(app.state, "inference_admission", controller, raising=False)
    provider = SlowProvider()
    set_inference_provider(provider)
    try:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "rusaai-cpu-smoke-test",
                "messages": [{"role": "user", "content": "Hello"}],
                "deadline_ms": 100,
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 504
    assert response.json()["detail"] == "Inference request deadline exceeded"
    assert provider.cancelled is True
    assert controller.active_count == 0
    assert controller.waiting_count == 0


def test_chat_completions_stream_emits_timeout_and_releases_slot(monkeypatch):
    class SlowStreamingProvider:
        cancelled = False

        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            return "unused"

        async def stream(self, messages, **parameters):
            yield InferenceStreamEvent(content="partial")
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            yield InferenceStreamEvent(finish_reason="stop")

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        SimpleNamespace(environment="development", model_name="rusaai-cpu-smoke-test"),
        raising=False,
    )
    controller = InferenceAdmissionController(max_active=1, max_waiting=1, queue_timeout=1)
    monkeypatch.setattr(app.state, "inference_admission", controller, raising=False)
    provider = SlowStreamingProvider()
    set_inference_provider(provider)
    try:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "rusaai-cpu-smoke-test",
                "messages": [{"role": "user", "content": "Hello"}],
                "deadline_ms": 200,
                "stream": True,
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 200
    assert '"content":"partial"' in response.text
    assert '"type":"timeout_error"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    assert provider.cancelled is True
    assert controller.active_count == 0
    assert controller.waiting_count == 0


def test_chat_completion_deadline_ms_is_bounded():
    for deadline_ms in (0, 3_600_001):
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "deadline_ms": deadline_ms,
            },
        )
        assert response.status_code == 422



def test_chat_completions_queue_deadline_returns_gateway_timeout(monkeypatch):
    class ExpiredAdmission:
        async def acquire(self, *, timeout_seconds=None):
            assert timeout_seconds is not None
            raise InferenceDeadlineTimeout("request deadline expired")

        async def release(self):
            raise AssertionError("a slot was never acquired")

    class MustNotGenerate:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            raise AssertionError("provider must not run after admission deadline")

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        SimpleNamespace(environment="development", model_name="rusaai-cpu-smoke-test"),
        raising=False,
    )
    monkeypatch.setattr(app.state, "inference_admission", ExpiredAdmission(), raising=False)
    set_inference_provider(MustNotGenerate())
    try:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "rusaai-cpu-smoke-test",
                "messages": [{"role": "user", "content": "Hello"}],
                "deadline_ms": 100,
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 504
    assert response.json()["detail"] == "Inference request deadline exceeded"



def test_expired_generation_deadline_closes_unstarted_coroutine():
    async def scenario():
        started = False

        async def generation():
            nonlocal started
            started = True
            return "too late"

        pending = generation()
        with pytest.raises(InferenceDeadlineTimeout):
            await _await_with_deadline(pending, time.monotonic() - 1)

        assert started is False
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED

    asyncio.run(scenario())



def test_cancelled_request_cancels_provider_coroutine():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def generation():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        request = asyncio.create_task(
            _await_with_deadline(generation(), time.monotonic() + 10)
        )
        await started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        assert cancelled.is_set()

    asyncio.run(scenario())


def test_streaming_response_rebinds_durable_admission_owner_task(monkeypatch):
    class LeaseAwareAdmission:
        def __init__(self):
            self.bound = False
            self.released = False

        async def acquire(self, *, timeout_seconds=None):
            return None

        def bind_owner_task(self):
            self.bound = True
            return True

        async def release(self):
            self.released = True

    class StreamingProvider:
        async def check_ready(self):
            return True

        async def complete(self, messages, **parameters):
            return "unused"

        async def stream(self, messages, **parameters):
            yield InferenceStreamEvent(content="streamed")
            yield InferenceStreamEvent(finish_reason="stop")

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        SimpleNamespace(environment="development", model_name="rusaai-cpu-smoke-test"),
        raising=False,
    )
    admission = LeaseAwareAdmission()
    monkeypatch.setattr(app.state, "inference_admission", admission, raising=False)
    set_inference_provider(StreamingProvider())
    try:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "rusaai-cpu-smoke-test",
                "messages": [{"role": "user", "content": "Stream"}],
                "stream": True,
            },
        )
    finally:
        set_inference_provider(None)

    assert response.status_code == 200
    assert '"content":"streamed"' in response.text
    assert admission.bound is True
    assert admission.released is True

