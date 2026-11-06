"""MCP Streamable HTTP client with explicit server and tool allowlists.

Only administrator-configured endpoints and tool names are exposed to the agent.
The model cannot add servers, change credentials, or discover unapproved tools.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import jsonschema

from .tools import ToolError, ToolRegistry

logger = logging.getLogger("rusa-agent-mcp")


class MCPProtocolError(RuntimeError):
    pass


class MCPRemoteServer:
    def __init__(
        self,
        alias: str,
        endpoint: str,
        token: str,
        allowed_tools: list[str],
        timeout_seconds: float = 30.0,
    ):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", alias):
            raise ValueError("MCP server alias must be a short lowercase identifier")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("MCP endpoints must use HTTPS except for local development")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Invalid MCP endpoint URL")
        if not allowed_tools:
            raise ValueError("MCP server must explicitly allow at least one tool")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) for name in allowed_tools):
            raise ValueError("MCP allowlist contains an invalid tool name")
        self.alias = alias
        self.endpoint = endpoint
        self.token = token
        self.allowed_tools = set(allowed_tools)
        self.timeout_seconds = timeout_seconds
        self.protocol_version = "2025-03-26"
        self.session_id: str | None = None
        self.client: httpx.AsyncClient | None = None
        self._request_id = 0
        self._schemas: dict[str, dict[str, Any]] = {}

    async def connect(self, registry: ToolRegistry) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=10.0)
        )
        try:
            initialized = await self._rpc(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "rusa-ai", "version": "1.0.0"},
                },
            )
            server_version = initialized.get("protocolVersion")
            if server_version != self.protocol_version:
                raise MCPProtocolError(
                    f"MCP server {self.alias} negotiated unsupported protocol version"
                )
            await self._notify("notifications/initialized")
            discovered = await self._list_tools()
            registered = 0
            for tool in discovered:
                name = tool.get("name")
                if name not in self.allowed_tools:
                    continue
                if not isinstance(name, str) or not re.fullmatch(
                    r"[A-Za-z0-9_.-]{1,128}", name
                ):
                    continue
                schema = tool.get("inputSchema", {"type": "object"})
                if not isinstance(schema, dict):
                    raise MCPProtocolError(f"MCP tool {name} has an invalid input schema")
                jsonschema.validators.validator_for(schema).check_schema(schema)
                self._schemas[name] = schema
                public_name = f"mcp.{self.alias}.{name}"

                async def handler(_tool_name=name, **arguments: Any) -> dict[str, Any]:
                    return await self.call_tool(_tool_name, arguments)

                registry.register(public_name, handler)
                registered += 1
            if registered == 0:
                raise MCPProtocolError(
                    f"MCP server {self.alias} exposed no tools from its allowlist"
                )
            logger.info(
                "Connected MCP server alias=%s registered_tools=%s",
                self.alias,
                registered,
            )
        except Exception:
            await self.close()
            raise

    async def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise MCPProtocolError("MCP client is not connected")
        response = await self.client.post(
            self.endpoint,
            headers=await self._headers(),
            json=payload,
        )
        if response.status_code >= 400:
            raise MCPProtocolError(
                f"MCP server {self.alias} returned HTTP {response.status_code}"
            )
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id
        if response.status_code == 202 or not response.content:
            return {}
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            data = response.json()
        elif "text/event-stream" in content_type:
            events = [
                line[5:].strip()
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
            data = None
            for event in events:
                if event and event != "[DONE]":
                    data = json.loads(event)
            if data is None:
                raise MCPProtocolError("MCP server returned an empty event stream")
        else:
            raise MCPProtocolError("MCP server returned an unsupported content type")
        if not isinstance(data, dict):
            raise MCPProtocolError("MCP response must be a JSON object")
        return data

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        response = await self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        if response.get("id") != request_id:
            raise MCPProtocolError("MCP response ID does not match the request")
        if "error" in response:
            raise MCPProtocolError(f"MCP request {method} failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(f"MCP request {method} returned an invalid result")
        return result

    async def _notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method})

    async def _list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            result = await self._rpc("tools/list", params)
            page = result.get("tools", [])
            if not isinstance(page, list):
                raise MCPProtocolError("MCP tools/list did not return a tools array")
            tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise MCPProtocolError("MCP server exceeded the configured pagination limit")

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name not in self.allowed_tools or tool_name not in self._schemas:
            raise ToolError("MCP tool is not allowlisted")
        try:
            jsonschema.validate(arguments, self._schemas[tool_name])
        except jsonschema.ValidationError as exc:
            raise ToolError("MCP tool arguments do not match the published schema") from exc
        result = await self._rpc(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        if result.get("isError"):
            raise ToolError(f"MCP tool {tool_name} returned an error")
        return {
            "server": self.alias,
            "tool": tool_name,
            "content": result.get("content", []),
            "structured_content": result.get("structuredContent"),
        }

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


class MCPGateway:
    def __init__(self, servers: list[MCPRemoteServer] | None = None):
        self.servers = servers or []

    @classmethod
    def from_env(cls) -> "MCPGateway":
        raw = os.getenv("RUSA_MCP_SERVERS", "{}")
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("RUSA_MCP_SERVERS must be valid JSON") from exc
        if not isinstance(config, dict):
            raise RuntimeError("RUSA_MCP_SERVERS must be a JSON object")
        servers = []
        for alias, entry in config.items():
            if not isinstance(entry, dict):
                raise RuntimeError(f"MCP server config {alias} must be an object")
            token_env = entry.get("token_env", "")
            token = os.getenv(token_env, "") if token_env else ""
            endpoint = entry.get("url", "")
            allow_tools = entry.get("allow_tools", [])
            if not isinstance(endpoint, str) or not isinstance(allow_tools, list):
                raise RuntimeError(f"MCP server config {alias} is invalid")
            servers.append(
                MCPRemoteServer(
                    alias=alias,
                    endpoint=endpoint,
                    token=token,
                    allowed_tools=allow_tools,
                )
            )
        return cls(servers)

    async def connect_and_register(self, registry: ToolRegistry) -> None:
        connected: list[MCPRemoteServer] = []
        try:
            for server in self.servers:
                await server.connect(registry)
                connected.append(server)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        for server in self.servers:
            await server.close()
