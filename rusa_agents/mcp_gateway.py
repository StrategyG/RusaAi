"""Tenant-aware adapter for allowlisted read-only MCP knowledge tools."""

from __future__ import annotations

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
        client_factory: Any = MCPToolClient,
    ) -> None:
        if not server_url:
            raise ValueError("MCP server URL is required")
        validate_server_url(server_url)
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
        tools = frozenset(allowed_tools or KNOWLEDGE_TOOLS)
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
