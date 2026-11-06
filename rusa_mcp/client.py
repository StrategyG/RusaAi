"""MCP Streamable HTTP client with strict endpoint and tool allowlists."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit
from typing import Any


class MCPToolError(RuntimeError):
    """Raised when an MCP tool call fails or violates local policy."""


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_server_url(
    server_url: str,
    *,
    allow_http_hosts: set[str] | frozenset[str] = frozenset(),
) -> str:
    parsed = urlsplit(server_url)
    secure = parsed.scheme == "https"
    local = (
        parsed.scheme == "http"
        and bool(parsed.hostname)
        and (_is_loopback(parsed.hostname) or parsed.hostname.lower() in allow_http_hosts)
    )
    if (
        not (secure or local)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/mcp")
    ):
        raise ValueError("MCP endpoint must be an HTTPS or loopback HTTP URL ending in /mcp")
    return server_url


class MCPToolClient:
    """One-call-per-session MCP client; model callers can invoke only configured tools."""

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        allowed_tools: set[str] | frozenset[str],
        allow_http_hosts: set[str] | frozenset[str] = frozenset(),
        timeout_seconds: float = 60.0,
        max_argument_bytes: int = 32_768,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self.server_url = validate_server_url(
            server_url,
            allow_http_hosts=frozenset(host.lower() for host in allow_http_hosts),
        )
        if not api_key or len(api_key) < 32 or len(api_key) > 512:
            raise ValueError("MCP bearer key must contain at least 32 characters")
        if not allowed_tools or any(not isinstance(name, str) or not name for name in allowed_tools):
            raise ValueError("an explicit non-empty MCP tool allowlist is required")
        if timeout_seconds <= 0 or max_argument_bytes < 1024 or max_response_bytes < 1024:
            raise ValueError("MCP client limits are invalid")
        self.api_key = api_key
        self.allowed_tools = frozenset(allowed_tools)
        self.timeout_seconds = timeout_seconds
        self.max_argument_bytes = max_argument_bytes
        self.max_response_bytes = max_response_bytes

    async def list_tools(self) -> list[dict[str, Any]]:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx2.Timeout(30.0, read=self.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            transport = streamable_http_client(self.server_url, http_client=http_client)
            async with Client(transport) as client:
                result = await client.list_tools()
                return [
                    {
                        "name": tool.name,
                        "title": tool.title,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in result.tools
                    if tool.name in self.allowed_tools
                ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.allowed_tools:
            raise MCPToolError(f"MCP tool '{name}' is not allowlisted")
        if not isinstance(arguments, dict):
            raise MCPToolError("MCP tool arguments must be an object")
        try:
            encoded_arguments = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MCPToolError("MCP tool arguments are not valid JSON data") from exc
        if len(encoded_arguments) > self.max_argument_bytes:
            raise MCPToolError("MCP tool arguments exceed the configured size limit")

        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx2.Timeout(30.0, read=self.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            transport = streamable_http_client(self.server_url, http_client=http_client)
            async with Client(transport) as client:
                listed = await client.list_tools()
                if name not in {tool.name for tool in listed.tools}:
                    raise MCPToolError(f"MCP server does not expose the allowlisted tool '{name}'")
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    raise MCPToolError(f"MCP tool '{name}' returned an error")

                if result.structured_content is not None:
                    payload = result.structured_content
                else:
                    from mcp.types import TextContent

                    texts = [
                        block.text for block in result.content
                        if isinstance(block, TextContent)
                    ]
                    payload = {"content": texts}

        try:
            encoded_result = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MCPToolError("MCP server returned a non-JSON structured result") from exc
        if len(encoded_result) > self.max_response_bytes:
            raise MCPToolError("MCP tool response exceeds the configured size limit")
        if not isinstance(payload, dict):
            return {"result": payload}
        return payload
