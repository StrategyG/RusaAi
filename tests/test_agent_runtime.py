from __future__ import annotations

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select

from rusa_agents.contracts import (
    AgentPlan,
    TaskCreate,
    TaskStatus,
    WorkerMessage,
    WorkerRole,
    validate_transition,
)
from rusa_agents.memory import MemoryStore
from rusa_agents.messaging import AgentMessageBus
from rusa_agents.orchestrator import HierarchicalOrchestrator, PlanRejected
from rusa_agents.policy import AutonomyPolicy
from rusa_agents.storage import Database, TaskRow, utcnow
from rusa_agents.tools import ToolError, ToolRegistry
from rusa_agents.worker import AgentRuntime


def make_task(**overrides) -> TaskCreate:
    data = {
        "objective": "Improve agent orchestration reliability",
        "repository": "StrategyG/RusaAi",
        "work_branch": "feature/agent-runtime-test",
        "acceptance_criteria": ["All required tests pass"],
    }
    data.update(overrides)
    return TaskCreate.model_validate(data)


class FakeProvider:
    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        return '{"steps": [], "expected_outcome": "test"}'


async def no_op_tool(**kwargs):
    return {"ok": True}


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'agent-test.db'}")
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def registry() -> ToolRegistry:
    tools = ToolRegistry()
    for name in {
        "github.list_files",
        "github.read_file",
        "github.create_branch",
        "github.write_file",
        "github.compare_branches",
        "github.get_ci_status",
        "github.create_pull_request",
    }:
        tools.register(name, no_op_tool)
    return tools


def valid_plan() -> AgentPlan:
    return AgentPlan.model_validate(
        {
            "steps": [
                {
                    "tool": "github.list_files",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "path": "",
                        "ref": "main",
                    },
                    "purpose": "Inspect repository structure",
                },
                {
                    "tool": "github.create_branch",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "base_branch": "main",
                        "work_branch": "feature/agent-runtime-test",
                    },
                    "purpose": "Create isolated work branch",
                },
                {
                    "tool": "github.write_file",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "path": "docs/example.md",
                        "content": "Example",
                        "message": "docs: add example",
                        "branch": "feature/agent-runtime-test",
                    },
                    "purpose": "Write the requested change",
                },
                {
                    "tool": "github.compare_branches",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "base": "main",
                        "head": "feature/agent-runtime-test",
                    },
                    "purpose": "Collect the complete bounded branch diff",
                },
                {
                    "tool": "agent.review_diff",
                    "arguments": {},
                    "purpose": "Independently review the branch diff and acceptance criteria",
                },
                {
                    "tool": "github.create_pull_request",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "title": "Example change",
                        "body": "Summary and test evidence",
                        "head": "feature/agent-runtime-test",
                        "base": "main",
                    },
                    "purpose": "Open a draft pull request",
                },
                {
                    "tool": "github.get_ci_status",
                    "arguments": {
                        "repository": "StrategyG/RusaAi",
                        "commit_sha": "$latest_write_commit",
                    },
                    "purpose": "Verify CI on the latest commit after PR creation",
                },
            ],
            "expected_outcome": "A reviewed draft pull request with passing checks",
        }
    )


def test_task_requires_safe_feature_branch():
    with pytest.raises(ValidationError):
        make_task(work_branch="main")
    with pytest.raises(ValidationError):
        make_task(work_branch="feature/../main")


def test_task_requires_branch_separation():
    with pytest.raises(ValidationError):
        make_task(base_branch="main", work_branch="main")
    with pytest.raises(ValidationError):
        make_task(base_branch="../main")
    with pytest.raises(ValidationError):
        make_task(work_branch="feature/unsafe branch")


def test_state_machine_rejects_invalid_transition():
    with pytest.raises(ValueError):
        validate_transition(TaskStatus.QUEUED, TaskStatus.SUCCEEDED)


def test_state_machine_allows_retry_from_failed():
    validate_transition(TaskStatus.FAILED, TaskStatus.QUEUED)


def test_policy_requires_approval_then_honors_it():
    policy = AutonomyPolicy()
    tool = "github.merge_pull_request"
    assert not policy.evaluate(tool, set(), set()).allowed
    assert not policy.evaluate(tool, {tool}, set()).allowed
    assert policy.evaluate(tool, {tool}, set()).requires_approval
    decision = policy.evaluate(tool, {tool}, {tool})
    assert decision.allowed
    assert not decision.requires_approval


def test_policy_never_allows_prohibited_tools():
    policy = AutonomyPolicy()
    decision = policy.evaluate("audit.disable", {"audit.disable"}, {"audit.disable"})
    assert not decision.allowed
    assert not decision.requires_approval


def test_plan_schema_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AgentPlan.model_validate(
            {
                "steps": [
                    {
                        "tool": "x",
                        "arguments": {},
                        "purpose": "x",
                        "extra": 1,
                    }
                ]
            }
        )


def test_orchestrator_accepts_task_scoped_plan(registry):
    orchestrator = HierarchicalOrchestrator(FakeProvider(), registry)
    orchestrator.validate_plan(valid_plan(), make_task().model_dump())


def test_orchestrator_rejects_repository_escape(registry):
    orchestrator = HierarchicalOrchestrator(FakeProvider(), registry)
    plan = valid_plan().model_dump()
    plan["steps"][0]["arguments"]["repository"] = "attacker/other"
    with pytest.raises(PlanRejected):
        orchestrator.validate_plan(AgentPlan.model_validate(plan), make_task().model_dump())


def test_orchestrator_rejects_write_before_branch(registry):
    orchestrator = HierarchicalOrchestrator(FakeProvider(), registry)
    plan = valid_plan().model_dump()
    plan["steps"][1], plan["steps"][2] = plan["steps"][2], plan["steps"][1]
    with pytest.raises(PlanRejected):
        orchestrator.validate_plan(AgentPlan.model_validate(plan), make_task().model_dump())


def test_tool_registry_rejects_unknown_tools():
    registry = ToolRegistry()
    with pytest.raises(ToolError):
        import asyncio

        asyncio.run(registry.execute("shell.execute", {}))


@pytest.mark.asyncio
async def test_submit_is_idempotent_and_tenant_scoped(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task = make_task().model_dump()
    first = await runtime.submit("tenant-a", "request-0001", task)
    repeated = await runtime.submit("tenant-a", "request-0001", task)
    other_tenant = await runtime.submit("tenant-b", "request-0001", task)
    assert first == repeated
    assert first != other_tenant
    from rusa_agents.worker import IdempotencyConflict

    with pytest.raises(IdempotencyConflict):
        await runtime.submit(
            "tenant-a",
            "request-0001",
            make_task(objective="Different objective with same key").model_dump(),
        )
    assert await runtime.get_task("tenant-b", first) is None
    assert await runtime.get_task("tenant-a", first) is not None


@pytest.mark.asyncio
async def test_memory_store_is_tenant_scoped(database):
    memory = MemoryStore(database)
    await memory.put(
        "tenant-a",
        "project",
        "architecture",
        "Hierarchical supervisor uses api_key=supersecretvalue",
        {"password": "plain-secret"},
    )
    stored = await memory.get("tenant-a", "project", "architecture")
    assert stored is not None
    assert "supersecretvalue" not in stored["content"]
    assert stored["metadata"]["password"] == "[REDACTED]"
    assert await memory.get("tenant-b", "project", "architecture") is None
    assert len(await memory.search("tenant-a", "supervisor")) == 1
    assert await memory.search("tenant-b", "supervisor") == []
    assert await memory.delete("tenant-a", "project", "architecture")
    assert await memory.get("tenant-a", "project", "architecture") is None


@pytest.mark.asyncio
async def test_message_bus_is_tenant_scoped(database):
    bus = AgentMessageBus(database)
    message = WorkerMessage(
        task_id="task-1",
        correlation_id="task-1",
        from_agent=WorkerRole.SUPERVISOR,
        to_agent=WorkerRole.ANALYST,
        message_type="task.delegate",
        payload={"objective": "inspect"},
    )
    await bus.publish("tenant-a", message)
    assert len(await bus.history("tenant-a", "task-1")) == 1
    assert await bus.history("tenant-b", "task-1") == []


@pytest.mark.asyncio
async def test_expired_worker_lease_is_recovered(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit("tenant-a", "request-0002", make_task().model_dump())
    async with database.session() as session:
        row = await session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        row.status = TaskStatus.RUNNING.value
        row.attempts = 1
        row.lease_owner = "dead-worker"
        row.lease_until = utcnow().replace(year=2000)
        await session.commit()
    recovered = await runtime.recover_expired_leases()
    task = await runtime.get_task("tenant-a", task_id)
    assert recovered == 1
    assert task["status"] == TaskStatus.QUEUED.value
    assert task["error"] == "Worker lease expired; task recovered by scheduler"


@pytest.mark.asyncio
async def test_claim_moves_queued_task_to_running(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit("tenant-a", "request-0003", make_task().model_dump())
    claimed = await runtime.claim()
    task = await runtime.get_task("tenant-a", task_id)
    assert claimed == task_id
    assert task["status"] == TaskStatus.RUNNING.value
    assert task["attempts"] == 1


class FakeEmbeddings:
    async def embed_query(self, query: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    async def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in documents]


class FakeVectorIndex:
    def __init__(self):
        self.records = {}

    async def upsert(self, tenant_id, source_id, chunks):
        self.records[(tenant_id, source_id)] = chunks

    async def search(self, tenant_id, embedding, top_k, allowed_source_ids=None):
        from rusa_agents.rag import RetrievedChunk

        results = []
        for (stored_tenant, source_id), chunks in self.records.items():
            if stored_tenant != tenant_id:
                continue
            if allowed_source_ids is not None and source_id not in allowed_source_ids:
                continue
            results.extend(
                RetrievedChunk(
                    chunk_id=chunk["chunk_id"],
                    source_id=source_id,
                    text=chunk["text"],
                    score=0.9,
                    metadata=chunk["metadata"],
                )
                for chunk in chunks
            )
        return results[:top_k]

    async def delete_source(self, tenant_id, source_id):
        return len(self.records.pop((tenant_id, source_id), []))


@pytest.mark.asyncio
async def test_rag_ingestion_retrieval_and_source_acl():
    from rusa_agents.rag import RAGService

    index = FakeVectorIndex()
    rag = RAGService(FakeEmbeddings(), index)
    text_value = ("RUSA-AI architecture api_key=supersecretvalue " * 180)
    chunks = await rag.ingest_text(
        "tenant-a",
        "design-doc",
        "Architecture",
        text_value,
        {"classification": "internal"},
    )
    assert chunks > 1
    assert "supersecretvalue" not in " ".join(
        item["text"] for item in index.records[("tenant-a", "design-doc")]
    )
    assert len(await rag.retrieve("tenant-a", "agent architecture")) == min(chunks, 5)
    assert await rag.retrieve(
        "tenant-a",
        "agent architecture",
        allowed_source_ids=["another-source"],
    ) == []
    assert await rag.retrieve("tenant-b", "agent architecture") == []
    assert await rag.delete_source("tenant-a", "design-doc") == chunks


@pytest.mark.asyncio
async def test_mcp_gateway_registers_only_allowlisted_tools(monkeypatch):
    from rusa_agents.mcp_gateway import MCPRemoteServer

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.status_code = status_code
            self.headers = {
                "content-type": "application/json",
                "Mcp-Session-Id": "test-session",
            }
            self._payload = payload
            self.content = b"{}" if status_code != 202 else b""

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs):
            self.closed = False

        async def post(self, url, headers, json):
            method = json["method"]
            request_id = json.get("id")
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "test"}}
            elif method == "notifications/initialized":
                return FakeResponse({}, status_code=202)
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "echo",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                                "additionalProperties": False,
                            },
                        },
                        {"name": "admin_delete", "inputSchema": {"type": "object"}},
                    ]
                }
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": json["params"]["arguments"]["message"]}]}
            else:
                raise AssertionError(f"Unexpected MCP method: {method}")
            return FakeResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("rusa_agents.mcp_gateway.httpx.AsyncClient", FakeClient)
    registry = ToolRegistry()
    server = MCPRemoteServer(
        alias="docs",
        endpoint="https://mcp.example.test/mcp",
        token="",
        allowed_tools=["echo"],
    )
    await server.connect(registry)
    assert registry.names() == {"mcp.docs.echo"}
    result = await registry.execute("mcp.docs.echo", {"message": "hello"})
    assert result["content"][0]["text"] == "hello"
    with pytest.raises(ToolError):
        await registry.execute("mcp.docs.echo", {"unexpected": "value"})
    await server.close()


def test_mcp_gateway_rejects_non_https_remote_endpoint():
    from rusa_agents.mcp_gateway import MCPRemoteServer

    with pytest.raises(ValueError):
        MCPRemoteServer(
            alias="unsafe",
            endpoint="http://remote.example.test/mcp",
            token="",
            allowed_tools=["search"],
        )


def test_tenant_and_approver_credentials_are_separate(monkeypatch):
    import json
    from fastapi import HTTPException
    from rusa_agents.api import resolve_approver_tenant, resolve_tenant

    tenant_secret = "tenant-secret-that-is-at-least-thirty-two-characters"
    approver_secret = "approver-secret-that-is-at-least-thirty-two-characters"
    monkeypatch.setenv(
        "RUSA_TENANT_API_KEYS",
        json.dumps({"tenant-a": tenant_secret}),
    )
    monkeypatch.setenv(
        "RUSA_TENANT_APPROVER_KEYS",
        json.dumps({"tenant-a": approver_secret}),
    )
    assert resolve_tenant(tenant_secret) == "tenant-a"
    assert resolve_approver_tenant(approver_secret) == "tenant-a"
    with pytest.raises(HTTPException):
        resolve_approver_tenant(tenant_secret)
    with pytest.raises(HTTPException):
        resolve_tenant(approver_secret)


@pytest.mark.asyncio
async def test_approver_can_resume_task_blocked_on_ci(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit("tenant-a", "request-0004", make_task().model_dump())
    plan = valid_plan().model_dump(mode="json")
    results = [
        {"tool": "github.list_files", "purpose": "inspect", "result": {"entries": []}},
        {"tool": "github.create_branch", "purpose": "branch", "result": {"sha": "a" * 40}},
        {"tool": "github.write_file", "purpose": "write", "result": {"commit_sha": "b" * 40}},
        {"tool": "github.compare_branches", "purpose": "diff", "result": {"files": []}},
        {"tool": "agent.review_diff", "purpose": "review", "result": {"approved": True}},
        {"tool": "github.create_pull_request", "purpose": "PR", "result": {"number": 1}},
        {"tool": "github.get_ci_status", "purpose": "CI", "result": {"commit_sha": "b" * 40, "all_passed": False}},
    ]
    async with database.session() as session:
        row = await session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        row.status = TaskStatus.BLOCKED.value
        row.plan = plan
        row.cursor = len(plan["steps"])
        row.tool_results = results
        row.error = "CI checks for the latest write have not all passed"
        await session.commit()

    await runtime.resume("tenant-a", task_id)
    task = await runtime.get_task("tenant-a", task_id)
    assert task["status"] == TaskStatus.QUEUED.value
    assert task["cursor"] == 6
    assert len(task["tool_results"]) == 6
    assert task["error"] is None


@pytest.mark.asyncio
async def test_blocked_task_with_unknown_reason_requires_operator_review(database, registry):
    runtime = AgentRuntime(database, FakeProvider(), registry)
    task_id = await runtime.submit("tenant-a", "request-0005", make_task().model_dump())
    async with database.session() as session:
        row = await session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        row.status = TaskStatus.BLOCKED.value
        row.plan = valid_plan().model_dump(mode="json")
        row.error = "No feature branch creation evidence"
        await session.commit()
    with pytest.raises(ValueError):
        await runtime.resume("tenant-a", task_id)


def test_github_tool_path_policy_blocks_secret_material():
    from rusa_agents.tools import ToolError, _assert_safe_file_path

    _assert_safe_file_path(".env.example")
    _assert_safe_file_path("docs/architecture.md")
    for path in (
        ".env",
        ".env.production",
        "secrets/token.json",
        ".ssh/id_rsa",
        "certs/private.pem",
    ):
        with pytest.raises(ToolError):
            _assert_safe_file_path(path)


class ExecutionProvider:
    def __init__(self, review_approved=True):
        self.review_approved = review_approved

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        import json

        if "independent Reviewer agent" in system_prompt:
            return json.dumps(
                {
                    "approved": self.review_approved,
                    "summary": "Review completed against the supplied acceptance criteria",
                    "findings": [] if self.review_approved else ["Potential issue requires human review"],
                    "risk_level": "low" if self.review_approved else "medium",
                }
            )

        return json.dumps(
            {
                "steps": [
                    {
                        "tool": "github.list_files",
                        "arguments": {
                            "repository": "StrategyG/RusaAi",
                            "path": "",
                            "ref": "main",
                        },
                        "purpose": "Inspect the repository",
                    },
                    {
                        "tool": "github.create_branch",
                        "arguments": {
                            "repository": "StrategyG/RusaAi",
                            "base_branch": "main",
                            "work_branch": "feature/agent-e2e-test",
                        },
                        "purpose": "Create isolated work branch",
                    },
                    {
                        "tool": "github.write_file",
                        "arguments": {
                            "repository": "StrategyG/RusaAi",
                            "path": "docs/e2e.md",
                            "content": "Validated worker execution",
                            "message": "docs: add e2e validation",
                            "branch": "feature/agent-e2e-test",
                        },
                        "purpose": "Write a test artifact",
                    },
                    {
                        "tool": "github.compare_branches",
                        "arguments": {
                            "repository": "StrategyG/RusaAi",
                            "base": "main",
                            "head": "feature/agent-e2e-test",
                        },
                        "purpose": "Collect branch diff for reviewer",
                    },
                    {
                        "tool": "agent.review_diff",
                        "arguments": {},
                        "purpose": "Review code changes before PR",
                    },
                    {
                        "tool": "github.create_pull_request",
                        "arguments": {
                            "repository": "StrategyG/RusaAi",
                            "title": "Agent E2E test",
                            "body": "Automated worker lifecycle test",
                            "head": "feature/agent-e2e-test",
                            "base": "main",
                        },
                        "purpose": "Create a draft pull request",
                    },
                    {
                        "tool": "github.get_ci_status",
                        "arguments": {
                            "repository": "StrategyG/RusaAi",
                            "commit_sha": "$latest_write_commit",
                        },
                        "purpose": "Verify the latest commit",
                    },
                ],
                "expected_outcome": "All task execution gates pass",
            }
        )


@pytest.mark.asyncio
async def test_worker_executes_full_plan_and_verifies_completion(database):
    async def list_files(**kwargs):
        return {"entries": [{"name": "README.md", "type": "file"}]}

    async def create_branch(**kwargs):
        return {"branch": kwargs["work_branch"], "sha": "a" * 40}

    async def write_file(**kwargs):
        return {"path": kwargs["path"], "commit_sha": "b" * 40}

    async def compare_branches(**kwargs):
        return {
            "status": "ahead",
            "ahead_by": 1,
            "behind_by": 0,
            "total_files": 1,
            "files": [{"filename": "docs/e2e.md", "patch": "+Validated worker execution"}],
        }

    async def create_pull_request(**kwargs):
        return {"number": 42, "draft": True}

    async def get_ci_status(**kwargs):
        return {
            "commit_sha": kwargs["commit_sha"],
            "all_passed": True,
            "checks": [{"name": "tests", "conclusion": "success"}],
        }

    tools = ToolRegistry()
    for name, handler in (
        ("github.list_files", list_files),
        ("github.create_branch", create_branch),
        ("github.write_file", write_file),
        ("github.compare_branches", compare_branches),
        ("github.create_pull_request", create_pull_request),
        ("github.get_ci_status", get_ci_status),
    ):
        tools.register(name, handler, input_schema={"type": "object"})

    runtime = AgentRuntime(database, ExecutionProvider(), tools)
    task_id = await runtime.submit(
        "tenant-e2e",
        "request-e2e-001",
        make_task(work_branch="feature/agent-e2e-test").model_dump(),
    )
    assert await runtime.run_once() is True

    task = await runtime.get_task("tenant-e2e", task_id)
    assert task["status"] == TaskStatus.SUCCEEDED.value
    assert task["cursor"] == 7
    assert task["result"]["verified"] is True
    assert task["result"]["acceptance_criteria_evaluated"] is False
    messages = await runtime.messages.history("tenant-e2e", task_id)
    assert any(message["message_type"] == "task.progress" for message in messages)
    assert any(message["message_type"] == "task.result" for message in messages)


def test_pgvector_index_enforces_embedding_dimension():
    from rusa_agents.rag import PostgresVectorIndex

    with pytest.raises(ValueError):
        PostgresVectorIndex._vector_literal([0.1, 0.2, 0.3])


@pytest.mark.asyncio
async def test_reviewer_rejection_pauses_until_approver_override(database):
    async def list_files(**kwargs):
        return {"entries": []}

    async def create_branch(**kwargs):
        return {"sha": "a" * 40}

    async def write_file(**kwargs):
        return {"commit_sha": "b" * 40}

    async def compare_branches(**kwargs):
        return {"files": [{"filename": "docs/review.md", "patch": "+change"}]}

    async def create_pull_request(**kwargs):
        return {"number": 9, "draft": True}

    async def get_ci_status(**kwargs):
        return {"commit_sha": kwargs["commit_sha"], "all_passed": True}

    tools = ToolRegistry()
    for name, handler in (
        ("github.list_files", list_files),
        ("github.create_branch", create_branch),
        ("github.write_file", write_file),
        ("github.compare_branches", compare_branches),
        ("github.create_pull_request", create_pull_request),
        ("github.get_ci_status", get_ci_status),
    ):
        tools.register(name, handler, input_schema={"type": "object"})

    runtime = AgentRuntime(database, ExecutionProvider(review_approved=False), tools)
    task_id = await runtime.submit(
        "tenant-review",
        "request-review-001",
        make_task(work_branch="feature/agent-e2e-test").model_dump(),
    )
    await runtime.run_once()
    task = await runtime.get_task("tenant-review", task_id)
    assert task["status"] == TaskStatus.AWAITING_APPROVAL.value
    assert task["pending_action"] == "agent.review_diff"

    await runtime.approve("tenant-review", task_id, "agent.review_diff")
    await runtime.run_once()
    task = await runtime.get_task("tenant-review", task_id)
    assert task["status"] == TaskStatus.SUCCEEDED.value
    review = next(
        item["result"]
        for item in task["tool_results"]
        if item["tool"] == "agent.review_diff"
    )
    assert review["human_override"] is True


def test_secret_redaction_handles_common_token_formats():
    from rusa_agents.security import redact_sensitive_data

    sanitized = redact_sensitive_data(
        {
            "api_key": "direct-secret",
            "message": "github token ghp_123456789012345678901234567890 and Bearer abcdefghijklmnop",
            "nested": {"password": "another-secret"},
        }
    )
    assert sanitized["api_key"] == "[REDACTED]"
    assert "direct-secret" not in str(sanitized)
    assert "123456789012345678901234567890" not in sanitized["message"]
    assert "abcdefghijklmnop" not in sanitized["message"]
    assert sanitized["nested"]["password"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_github_branch_creation_is_idempotent(monkeypatch):
    from rusa_agents.tools import GitHubTools

    github = GitHubTools(token="test-token")
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path))
        return {"object": {"sha": "a" * 40}}

    monkeypatch.setattr(github, "_request", fake_request)
    result = await github.create_branch(
        "StrategyG/RusaAi",
        "main",
        "feature/already-created",
    )
    assert result["already_exists"] is True
    assert result["sha"] == "a" * 40
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_github_pull_request_creation_reuses_existing_open_pr(monkeypatch):
    from rusa_agents.tools import GitHubTools

    github = GitHubTools(token="test-token")

    async def fake_request(method, path, **kwargs):
        assert method == "GET"
        assert path.endswith("/pulls")
        return [{"number": 12, "html_url": "https://github.com/StrategyG/RusaAi/pull/12", "state": "open", "draft": True}]

    monkeypatch.setattr(github, "_request", fake_request)
    result = await github.create_pull_request(
        "StrategyG/RusaAi",
        "Title",
        "Body",
        "feature/existing",
        "main",
    )
    assert result["number"] == 12
    assert result["already_exists"] is True


@pytest.mark.asyncio
async def test_github_file_write_detects_already_applied_content(monkeypatch):
    import base64
    from rusa_agents.tools import GitHubTools

    github = GitHubTools(token="test-token")
    content = "same content"

    async def fake_request(method, path, **kwargs):
        if method == "GET" and "/contents/" in path:
            return {
                "sha": "c" * 40,
                "content": base64.b64encode(content.encode()).decode(),
            }
        if method == "GET" and path.endswith("/commits"):
            return [{"sha": "d" * 40}]
        raise AssertionError(f"Unexpected GitHub request: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    result = await github.write_file(
        "StrategyG/RusaAi",
        "docs/idempotent.md",
        content,
        "docs: add idempotent file",
        "feature/idempotent",
    )
    assert result["already_applied"] is True
    assert result["commit_sha"] == "d" * 40


@pytest.mark.asyncio
async def test_github_file_update_uses_optimistic_current_sha(monkeypatch):
    import base64
    from rusa_agents.tools import GitHubTools

    github = GitHubTools(token="test-token")
    content = "updated content"
    observed = {}

    async def fake_request(method, path, **kwargs):
        if method == "GET" and "/contents/" in path:
            return {
                "sha": "c" * 40,
                "content": base64.b64encode(b"old content").decode(),
            }
        if method == "PUT" and "/contents/" in path:
            observed.update(kwargs["json"])
            return {
                "commit": {"sha": "d" * 40},
                "content": {"sha": "e" * 40},
            }
        raise AssertionError(f"Unexpected GitHub request: {method} {path}")

    monkeypatch.setattr(github, "_request", fake_request)
    result = await github.write_file(
        "StrategyG/RusaAi",
        "docs/update.md",
        content,
        "docs: update file",
        "feature/update",
    )
    assert observed["sha"] == "c" * 40
    assert result["commit_sha"] == "d" * 40
    assert result["already_applied"] is False
