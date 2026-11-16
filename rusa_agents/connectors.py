"""Administrator-managed, tenant-isolated remote connector configuration."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

from rusa_mcp.client import MCPToolClient

from .external_rag import ExternalRAGClient


def _tenant_connection_map(environ: Mapping[str, str], variable: str) -> dict[str, Any]:
    try:
        configured = json.loads(environ.get(variable, "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{variable} must be a JSON object") from exc
    if not isinstance(configured, dict):
        raise RuntimeError(f"{variable} must map tenant IDs to connection objects")
    for tenant_id, connection in configured.items():
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or tenant_id != tenant_id.strip()
            or len(tenant_id) > 128
            or not isinstance(connection, dict)
        ):
            raise RuntimeError(f"{variable} contains an invalid tenant or connection")
    return configured


def load_external_rag_clients(
    environ: Mapping[str, str] | None = None,
) -> dict[str, ExternalRAGClient]:
    """Build one externally authenticated retrieval client per tenant."""
    env = os.environ if environ is None else environ
    configured = _tenant_connection_map(env, "RUSA_EXTERNAL_RAG_CONNECTIONS_JSON")
    clients: dict[str, ExternalRAGClient] = {}
    credential_envs: set[str] = set()
    bearer_tokens: set[str] = set()
    allowed_fields = {
        "endpoint", "allowed_hosts", "credential_env", "timeout_seconds",
        "max_response_bytes",
    }

    for tenant_id, connection in configured.items():
        if set(connection) - allowed_fields:
            raise RuntimeError(f"External RAG connection for tenant {tenant_id} has unsupported settings")
        endpoint = connection.get("endpoint")
        allowed_hosts = connection.get("allowed_hosts")
        credential_env = connection.get("credential_env")
        if (
            not isinstance(endpoint, str)
            or not isinstance(allowed_hosts, list)
            or not allowed_hosts
            or any(not isinstance(host, str) or not host.strip() for host in allowed_hosts)
            or not isinstance(credential_env, str)
            or not re.fullmatch(r"RUSA_EXTERNAL_RAG_[A-Z0-9_]{1,64}", credential_env)
        ):
            raise RuntimeError(f"External RAG connection for tenant {tenant_id} is incomplete or invalid")
        if credential_env in credential_envs:
            raise RuntimeError("Each tenant external RAG connection must use a distinct credential variable")
        credential_envs.add(credential_env)
        token = env.get(credential_env, "")
        if not token or token in bearer_tokens:
            raise RuntimeError("Each tenant external RAG connection must use a distinct non-empty token")
        bearer_tokens.add(token)
        try:
            clients[tenant_id] = ExternalRAGClient(
                endpoint=endpoint,
                bearer_token=token,
                allowed_hosts=set(allowed_hosts),
                timeout_seconds=float(connection.get("timeout_seconds", 15)),
                max_response_bytes=int(connection.get("max_response_bytes", 1_000_000)),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"External RAG connection for tenant {tenant_id} failed validation"
            ) from exc
    return clients


def load_mcp_clients(
    environ: Mapping[str, str] | None = None,
) -> dict[str, MCPToolClient]:
    """Build one allowlisted read-only MCP client per tenant."""
    env = os.environ if environ is None else environ
    configured = _tenant_connection_map(env, "RUSA_MCP_CONNECTIONS_JSON")
    clients: dict[str, MCPToolClient] = {}
    credential_envs: set[str] = set()
    bearer_tokens: set[str] = set()
    allowed_fields = {
        "server_url", "allowed_hosts", "credential_env", "read_only_tools",
        "allow_http_hosts", "timeout_seconds", "max_argument_bytes",
        "max_response_bytes",
    }

    if configured:
        try:
            import httpx2  # noqa: F401
            import mcp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "MCP connections are configured but MCP worker dependencies are missing"
            ) from exc

    for tenant_id, connection in configured.items():
        if set(connection) - allowed_fields:
            raise RuntimeError(f"MCP connection for tenant {tenant_id} has unsupported settings")
        endpoint = connection.get("server_url")
        allowed_hosts = connection.get("allowed_hosts")
        credential_env = connection.get("credential_env")
        read_only_tools = connection.get("read_only_tools")
        allow_http_hosts = connection.get("allow_http_hosts", [])
        if (
            not isinstance(endpoint, str)
            or not isinstance(allowed_hosts, list)
            or not allowed_hosts
            or any(not isinstance(host, str) or not host.strip() for host in allowed_hosts)
            or not isinstance(read_only_tools, list)
            or not read_only_tools
            or any(not isinstance(name, str) or not name.strip() for name in read_only_tools)
            or not isinstance(allow_http_hosts, list)
            or any(not isinstance(host, str) or not host.strip() for host in allow_http_hosts)
            or not isinstance(credential_env, str)
            or not re.fullmatch(r"RUSA_MCP_[A-Z0-9_]{1,64}", credential_env)
        ):
            raise RuntimeError(f"MCP connection for tenant {tenant_id} is incomplete or invalid")
        if credential_env in credential_envs:
            raise RuntimeError("Each tenant MCP connection must use a distinct credential variable")
        credential_envs.add(credential_env)
        token = env.get(credential_env, "")
        if not token or token in bearer_tokens:
            raise RuntimeError("Each tenant MCP connection must use a distinct non-empty bearer token")
        bearer_tokens.add(token)
        try:
            clients[tenant_id] = MCPToolClient(
                server_url=endpoint,
                api_key=token,
                allowed_tools=set(read_only_tools),
                allowed_hosts=set(allowed_hosts),
                allow_http_hosts=set(allow_http_hosts),
                timeout_seconds=float(connection.get("timeout_seconds", 15)),
                max_argument_bytes=int(connection.get("max_argument_bytes", 32_768)),
                max_response_bytes=int(connection.get("max_response_bytes", 1_000_000)),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"MCP connection for tenant {tenant_id} failed validation") from exc
    return clients
