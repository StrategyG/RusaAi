import asyncio

from rusa_agents.contracts import CodingTask, RepositoryTarget, ToolCall
from rusa_agents.mcp_gateway import LocalKnowledgeGateway, MCPKnowledgeGateway
from rusa_agents.policy import AutonomyPolicy
from rusa_agents.rag_memory import CompositeMemoryReader
from rusa_agents.tool_router import CompositeToolGateway
from rusa_agents.workers import AgentWorker
from rusa_agents.worker import _enabled_tool_set
from rusa_agents.contracts import WorkerRole


class FakeEpisodic:
    def search(self, tenant_id, query, *, limit=5):
        return [{"content": "prior task", "source": "task:1", "metadata": {}}]


class FakeKnowledge:
    def search(self, **kwargs):
        return [{
            "content": "retrieved project guidance",
            "source_uri": "docs/guide.md",
            "title": "Guide",
            "document_id": "doc-1",
            "score": 0.02,
        }]

    def get_document(self, **kwargs):
        return {"document_id": kwargs["document_id"], "content": "document body"}

    def list_collections(self, **kwargs):
        return [{"collection": "agent", "document_count": 1}]

    def close(self):
        pass


class FakeMCPClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def call_tool(self, name, arguments):
        return {"query": arguments.get("query"), "results": [{"content": "mcp evidence"}]}


class FakeGitHub:
    async def execute(self, task, call):
        return "github-result"


def _task():
    return CodingTask(
        tenant_id="tenant-a",
        objective="Inspect the repository",
        repository=RepositoryTarget("StrategyG/RusaAi"),
        acceptance_criteria=("Evidence is retrieved",),
    )


def test_composite_memory_includes_episodic_and_rag_evidence():
    reader = CompositeMemoryReader(FakeEpisodic(), FakeKnowledge(), collection="agent")
    results = reader.search("tenant-a", "repository architecture", limit=2)
    assert [item["memory_type"] for item in results] == ["episodic", "knowledge"]
    assert results[1]["source"] == "docs/guide.md"


def test_mcp_gateway_is_tenant_scoped_and_read_only():
    gateway = MCPKnowledgeGateway(
        server_url="https://knowledge.example.com/mcp",
        tenant_tokens={"tenant-a": "t" * 40},
        client_factory=FakeMCPClient,
    )
    result = asyncio.run(
        gateway.execute(
            _task(),
            ToolCall(name="search_knowledge", arguments={"query": "architecture"}),
        )
    )
    assert result.success is True
    assert result.data["results"][0]["content"] == "mcp evidence"
    assert result.evidence["tenant_scoped"] is True


def test_policy_allows_read_only_knowledge_action():
    decision = AutonomyPolicy().evaluate("knowledge_read", repository=_task().repository)
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_composite_tool_gateway_routes_knowledge_tools():
    class FakeKnowledgeGateway:
        async def execute(self, task, call):
            return "knowledge-result"

    router = CompositeToolGateway(FakeGitHub(), FakeKnowledgeGateway())
    result = asyncio.run(
        router.execute(
            _task(),
            ToolCall(name="search_knowledge", arguments={"query": "x"}),
        )
    )
    assert result == "knowledge-result"


def test_local_knowledge_gateway_executes_tenant_scoped_search():
    gateway = LocalKnowledgeGateway(FakeKnowledge(), default_collection="agent")
    result = asyncio.run(
        gateway.execute(
            _task(),
            ToolCall(name="search_knowledge", arguments={"query": "architecture"}),
        )
    )
    assert result.success is True
    assert result.data["results"][0]["document_id"] == "doc-1"
    assert result.evidence["source"] == "local_rag"


def test_remote_mcp_tool_subset_is_reflected_in_worker_allowlist():
    enabled = _enabled_tool_set({"search_knowledge"})
    worker = AgentWorker(
        WorkerRole.ANALYST,
        FakeProvider(),
        enabled_tools=enabled,
    )
    assert "search_knowledge" in worker.allowed_tools
    assert "get_document" not in worker.allowed_tools
    assert "list_collections" not in worker.allowed_tools


def test_worker_tool_set_rejects_unapproved_knowledge_tool():
    try:
        _enabled_tool_set({"search_knowledge", "execute_shell"})
    except ValueError as exc:
        assert "unapproved" in str(exc)
    else:
        raise AssertionError("unapproved tool was accepted")


def test_worker_tool_allowlist_can_disable_unconfigured_knowledge_tools():
    class FakeProvider:
        async def complete(self, *, system_prompt, user_prompt):
            return "{}"

    worker = AgentWorker(
        WorkerRole.ANALYST,
        FakeProvider(),
        enabled_tools={"read_file", "list_files"},
    )
    assert worker.allowed_tools == frozenset({"read_file", "list_files"})
    assert "search_knowledge" not in worker.allowed_tools
