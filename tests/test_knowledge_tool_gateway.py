import pytest

from rusa_agents.contracts import WorkerRole
from rusa_agents.tool_gateway import ToolDenied, WorkspaceToolGateway


class FakeKnowledgeService:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return [{"document_id": "doc-1", "source_uri": "kb://guide", "content": "evidence"}]

    def get_document(self, **kwargs):
        self.calls.append(("get_document", kwargs))
        return {"document_id": kwargs["document_id"], "content": "document text"}

    def list_collections(self, **kwargs):
        self.calls.append(("list_collections", kwargs))
        return [{"collection": "agent", "document_count": 1}]


def test_knowledge_tools_are_scoped_to_authenticated_task_tenant(tmp_path):
    service = FakeKnowledgeService()
    gateway = WorkspaceToolGateway(
        tmp_path,
        knowledge_service=service,
        knowledge_tenant_id="tenant-from-queue",
        knowledge_collection="agent",
        knowledge_project_id="strategyg--rusaai",
    )

    result = gateway.execute(
        {"name": "search_knowledge", "arguments": {"query": "deployment guide", "limit": 3}},
        WorkerRole.ANALYST,
    )
    assert result.ok
    assert "kb://guide" in result.output
    assert service.calls[0][1]["tenant_id"] == "tenant-from-queue"
    assert service.calls[0][1]["collection"] == "agent"
    assert service.calls[0][1]["agent_id"] == "aicode-analyst"
    assert service.calls[0][1]["project_id"] == "strategyg--rusaai"


def test_knowledge_tool_cannot_override_tenant_or_pass_unknown_arguments(tmp_path):
    gateway = WorkspaceToolGateway(
        tmp_path,
        knowledge_service=FakeKnowledgeService(),
        knowledge_tenant_id="tenant-a",
    )
    with pytest.raises(ToolDenied, match="Unexpected arguments"):
        gateway.execute(
            {
                "name": "search_knowledge",
                "arguments": {"query": "secret", "tenant_id": "tenant-b"},
            },
            WorkerRole.ANALYST,
        )


def test_knowledge_tools_fail_closed_when_rag_is_disabled(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied, match="not enabled"):
        gateway.execute(
            {"name": "list_collections", "arguments": {}},
            WorkerRole.ANALYST,
        )


def test_worker_cannot_choose_collection_and_document_reads_keep_agent_scope(tmp_path):
    service = FakeKnowledgeService()
    gateway = WorkspaceToolGateway(
        tmp_path,
        knowledge_service=service,
        knowledge_tenant_id="tenant-a",
        knowledge_collection="agent",
        knowledge_project_id="strategyg--rusaai",
    )
    with pytest.raises(ToolDenied, match="Unexpected arguments"):
        gateway.execute(
            {
                "name": "search_knowledge",
                "arguments": {"query": "deployment", "collection": "other"},
            },
            WorkerRole.ANALYST,
        )

    result = gateway.execute(
        {
            "name": "get_document",
            "arguments": {"document_id": "00000000-0000-0000-0000-000000000001"},
        },
        WorkerRole.SECURITY,
    )
    assert result.ok
    call = next(call for call in service.calls if call[0] == "get_document")
    assert call[1]["collection"] == "agent"
    assert call[1]["agent_id"] == "aicode-security"
    assert call[1]["project_id"] == "strategyg--rusaai"


def test_list_collections_is_agent_and_project_scoped(tmp_path):
    service = FakeKnowledgeService()
    gateway = WorkspaceToolGateway(
        tmp_path,
        knowledge_service=service,
        knowledge_tenant_id="tenant-a",
        knowledge_collection="agent",
        knowledge_project_id="strategyg--rusaai",
    )
    result = gateway.execute(
        {"name": "list_collections", "arguments": {}},
        WorkerRole.SECURITY,
    )
    assert result.ok
    call = next(call for call in service.calls if call[0] == "list_collections")
    assert call[1] == {
        "tenant_id": "tenant-a",
        "agent_id": "aicode-security",
        "project_id": "strategyg--rusaai",
    }


def test_gateway_rejects_invalid_project_scope(tmp_path):
    with pytest.raises(ValueError, match="knowledge project identity"):
        WorkspaceToolGateway(
            tmp_path,
            knowledge_service=FakeKnowledgeService(),
            knowledge_tenant_id="tenant-a",
            knowledge_project_id="../tenant-b",
        )
