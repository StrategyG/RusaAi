"""Tenant-aware adapter for allowlisted read-only MCP knowledge tools."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit
from typing import Any

from .contracts import CodingTask, ToolCall, ToolResult
from rusa_mcp.client import MCPToolClient, validate_server_url

KNOWLEDGE_TOOLS = frozenset({"search_knowledge", "get_document", "list_collections"})
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class MCPKnowledgeGateway:
    def __init__(
        self,
        *,
        server_url: str,
        tenant_tokens: dict[str, str],
        allowed_tools: set[str] | None = None,
        allow_http_hosts: set[str] | None = None,
        client_factory: Any = MCPToolClient,
    ) -> None:
        if not server_url:
            raise ValueError("MCP server URL is required")
        self.allow_http_hosts = {
            host.strip().lower() for host in (allow_http_hosts or set()) if host.strip()
        }
        validate_server_url(server_url, allow_http_hosts=self.allow_http_hosts)
        if not tenant_tokens:
            raise ValueError("per-tenant MCP bearer tokens are required")
        if any(
            not isinstance(tenant, str)
            or not TENANT_PATTERN.fullmatch(tenant)
            or not isinstance(token, str)
            or len(token) < 32
            for tenant, token in tenant_tokens.items()
        ):
            raise ValueError("MCP tenant token map is invalid")
        tools = frozenset(KNOWLEDGE_TOOLS if allowed_tools is None else allowed_tools)
        if not tools or not tools.issubset(KNOWLEDGE_TOOLS):
            raise ValueError("MCP gateway may expose only approved read-only knowledge tools")
        self.server_url = server_url
        self.tenant_tokens = dict(tenant_tokens)
        self.allowed_tools = tools
        self.client_factory = client_factory

    async def execute(self, task: CodingTask, call: ToolCall) -> ToolResult:
        if call.name not in self.allowed_tools:
            raise PermissionError(f"MCP tool '{call.name}' is not allowlisted")
        token = self.tenant_tokens.get(task.tenant_id)
        if token is None:
            raise PermissionError("no MCP credential is configured for this tenant")
        client = self.client_factory(
            server_url=self.server_url,
            api_key=token,
            allowed_tools=set(self.allowed_tools),
            allow_http_hosts=set(self.allow_http_hosts),
        )
        result = await client.call_tool(call.name, dict(call.arguments))
        hostname = urlsplit(self.server_url).hostname or "unknown"
        return ToolResult(
            name=call.name,
            success=True,
            data=result,
            evidence={
                "source": "mcp_streamable_http",
                "server_host": hostname,
                "tool": call.name,
                "tenant_scoped": True,
            },
        )



class LocalKnowledgeGateway:
    """Direct local RAG adapter used when no remote MCP endpoint is configured."""

    def __init__(self, knowledge_service: Any, *, default_collection: str = "agent") -> None:
        self.knowledge_service = knowledge_service
        self.default_collection = default_collection

    def _execute_sync(self, task: CodingTask, call: ToolCall) -> ToolResult:
        if call.name not in KNOWLEDGE_TOOLS:
            raise PermissionError(f"knowledge tool '{call.name}' is not allowlisted")
        arguments = dict(call.arguments)
        if call.name == "search_knowledge":
            if set(arguments) - {"query", "collection", "limit"}:
                raise ValueError("search_knowledge received unexpected arguments")
            query = arguments.get("query")
            collection = arguments.get("collection", self.default_collection)
            limit = arguments.get("limit", 5)
            if not isinstance(query, str) or type(limit) is not int:
                raise ValueError("search_knowledge arguments are invalid")
            data = {
                "query": query,
                "collection": collection,
                "results": self.knowledge_service.search(
                    tenant_id=task.tenant_id,
                    collection=collection,
                    query=query,
                    limit=limit,
                ),
            }
        elif call.name == "get_document":
            if set(arguments) - {"document_id", "collection"}:
                raise ValueError("get_document received unexpected arguments")
            document_id = arguments.get("document_id")
            collection = arguments.get("collection", self.default_collection)
            if not isinstance(document_id, str):
                raise ValueError("get_document requires a document_id string")
            document = self.knowledge_service.get_document(
                tenant_id=task.tenant_id,
                collection=collection,
                document_id=document_id,
                max_chars=20_000,
            )
            data = {"found": document is not None, "document": document}
        else:
            if arguments:
                raise ValueError("list_collections takes no arguments")
            data = {
                "collections": self.knowledge_service.list_collections(
                    tenant_id=task.tenant_id,
                )
            }
        return ToolResult(
            name=call.name,
            success=True,
            data=data,
            evidence={"source": "local_rag", "tool": call.name, "tenant_scoped": True},
        )

    async def execute(self, task: CodingTask, call: ToolCall) -> ToolResult:
        return await asyncio.to_thread(self._execute_sync, task, call)
