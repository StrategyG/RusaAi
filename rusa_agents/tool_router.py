"""Composite tool gateway routing GitHub operations and read-only MCP tools."""

from __future__ import annotations

from typing import Any

from .contracts import CodingTask, ToolCall, ToolResult
from .mcp_gateway import KNOWLEDGE_TOOLS


class CompositeToolGateway:
    def __init__(self, github_gateway: Any, mcp_gateway: Any) -> None:
        self.github_gateway = github_gateway
        self.mcp_gateway = mcp_gateway

    async def execute(self, task: CodingTask, call: ToolCall) -> ToolResult:
        if call.name in KNOWLEDGE_TOOLS:
            return await self.mcp_gateway.execute(task, call)
        return await self.github_gateway.execute(task, call)
