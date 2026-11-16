import sys
from types import ModuleType

import pytest

from rusa_agents.connectors import load_external_rag_clients, load_mcp_clients


def test_external_rag_connections_are_isolated_per_tenant():
    environ = {
        "RUSA_EXTERNAL_RAG_CONNECTIONS_JSON": (
            '{"tenant-a":{"endpoint":"https://rag-a.example.org/v1/retrieve",'
            '"allowed_hosts":["rag-a.example.org"],'
            '"credential_env":"RUSA_EXTERNAL_RAG_A_TOKEN"},'
            '"tenant-b":{"endpoint":"https://rag-b.example.org/v1/retrieve",'
            '"allowed_hosts":["rag-b.example.org"],'
            '"credential_env":"RUSA_EXTERNAL_RAG_B_TOKEN"}}'
        ),
        "RUSA_EXTERNAL_RAG_A_TOKEN": "a" * 40,
        "RUSA_EXTERNAL_RAG_B_TOKEN": "b" * 40,
    }
    clients = load_external_rag_clients(environ)

    assert set(clients) == {"tenant-a", "tenant-b"}
    assert clients["tenant-a"].endpoint == "https://rag-a.example.org/v1/retrieve"
    assert clients["tenant-a"]._token == "a" * 40
    assert clients["tenant-b"]._token == "b" * 40


def test_external_rag_rejects_shared_tenant_credentials():
    environ = {
        "RUSA_EXTERNAL_RAG_CONNECTIONS_JSON": (
            '{"tenant-a":{"endpoint":"https://rag-a.example.org/v1/retrieve",'
            '"allowed_hosts":["rag-a.example.org"],"credential_env":"RUSA_EXTERNAL_RAG_A_TOKEN"},'
            '"tenant-b":{"endpoint":"https://rag-b.example.org/v1/retrieve",'
            '"allowed_hosts":["rag-b.example.org"],"credential_env":"RUSA_EXTERNAL_RAG_B_TOKEN"}}'
        ),
        "RUSA_EXTERNAL_RAG_A_TOKEN": "same-token-" + "a" * 40,
        "RUSA_EXTERNAL_RAG_B_TOKEN": "same-token-" + "a" * 40,
    }
    with pytest.raises(RuntimeError, match="distinct"):
        load_external_rag_clients(environ)


def test_external_rag_rejects_endpoint_outside_hostname_allowlist():
    environ = {
        "RUSA_EXTERNAL_RAG_CONNECTIONS_JSON": (
            '{"tenant-a":{"endpoint":"https://unexpected.example.org/v1/retrieve",'
            '"allowed_hosts":["rag-a.example.org"],'
            '"credential_env":"RUSA_EXTERNAL_RAG_A_TOKEN"}}'
        ),
        "RUSA_EXTERNAL_RAG_A_TOKEN": "a" * 40,
    }
    with pytest.raises(RuntimeError, match="failed validation"):
        load_external_rag_clients(environ)


def test_mcp_connections_are_isolated_per_tenant(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", ModuleType("httpx2"))
    monkeypatch.setitem(sys.modules, "mcp", ModuleType("mcp"))
    environ = {
        "RUSA_MCP_CONNECTIONS_JSON": (
            '{"tenant-a":{"server_url":"https://mcp-a.example.org/mcp",'
            '"allowed_hosts":["mcp-a.example.org"],"credential_env":"RUSA_MCP_A_TOKEN",'
            '"read_only_tools":["search_docs"]},'
            '"tenant-b":{"server_url":"https://mcp-b.example.org/mcp",'
            '"allowed_hosts":["mcp-b.example.org"],"credential_env":"RUSA_MCP_B_TOKEN",'
            '"read_only_tools":["lookup_records"]}}'
        ),
        "RUSA_MCP_A_TOKEN": "a" * 40,
        "RUSA_MCP_B_TOKEN": "b" * 40,
    }
    clients = load_mcp_clients(environ)

    assert set(clients) == {"tenant-a", "tenant-b"}
    assert clients["tenant-a"].server_url == "https://mcp-a.example.org/mcp"
    assert clients["tenant-a"].allowed_tools == frozenset({"search_docs"})
    assert clients["tenant-b"].allowed_tools == frozenset({"lookup_records"})


def test_mcp_rejects_shared_tenant_credentials(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", ModuleType("httpx2"))
    monkeypatch.setitem(sys.modules, "mcp", ModuleType("mcp"))
    environ = {
        "RUSA_MCP_CONNECTIONS_JSON": (
            '{"tenant-a":{"server_url":"https://mcp-a.example.org/mcp",'
            '"allowed_hosts":["mcp-a.example.org"],"credential_env":"RUSA_MCP_A_TOKEN",'
            '"read_only_tools":["search_docs"]},'
            '"tenant-b":{"server_url":"https://mcp-b.example.org/mcp",'
            '"allowed_hosts":["mcp-b.example.org"],"credential_env":"RUSA_MCP_B_TOKEN",'
            '"read_only_tools":["lookup_records"]}}'
        ),
        "RUSA_MCP_A_TOKEN": "same-token-" + "a" * 40,
        "RUSA_MCP_B_TOKEN": "same-token-" + "a" * 40,
    }
    with pytest.raises(RuntimeError, match="distinct"):
        load_mcp_clients(environ)


def test_mcp_rejects_host_not_in_administrator_allowlist(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", ModuleType("httpx2"))
    monkeypatch.setitem(sys.modules, "mcp", ModuleType("mcp"))
    environ = {
        "RUSA_MCP_CONNECTIONS_JSON": (
            '{"tenant-a":{"server_url":"https://unexpected.example.org/mcp",'
            '"allowed_hosts":["mcp-a.example.org"],"credential_env":"RUSA_MCP_A_TOKEN",'
            '"read_only_tools":["search_docs"]}}'
        ),
        "RUSA_MCP_A_TOKEN": "a" * 40,
    }
    with pytest.raises(RuntimeError, match="failed validation"):
        load_mcp_clients(environ)
