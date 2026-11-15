import pytest

from rusa_mcp.client import MCPToolClient, validate_server_url


def test_mcp_url_requires_secure_transport_or_loopback():
    assert validate_server_url(
        "https://knowledge.example.com/mcp",
        allowed_hosts={"knowledge.example.com"},
    )
    assert validate_server_url(
        "http://127.0.0.1:8002/mcp",
        allowed_hosts={"127.0.0.1"},
    )
    assert validate_server_url(
        "http://rusa-mcp:8002/mcp",
        allowed_hosts={"rusa-mcp"},
        allow_http_hosts={"rusa-mcp"},
    )
    with pytest.raises(ValueError):
        validate_server_url(
            "http://knowledge.example.com/mcp",
            allowed_hosts={"knowledge.example.com"},
        )
    with pytest.raises(ValueError):
        validate_server_url(
            "https://knowledge.example.com/other",
            allowed_hosts={"knowledge.example.com"},
        )


def test_mcp_client_requires_explicit_tool_allowlist():
    with pytest.raises(ValueError):
        MCPToolClient(
            server_url="https://knowledge.example.com/mcp",
            api_key="x" * 40,
            allowed_tools=set(),
            allowed_hosts={"knowledge.example.com"},
        )


def test_mcp_client_requires_long_bearer_token():
    with pytest.raises(ValueError):
        MCPToolClient(
            server_url="https://knowledge.example.com/mcp",
            api_key="short",
            allowed_tools={"search_knowledge"},
            allowed_hosts={"knowledge.example.com"},
        )


def test_mcp_https_endpoint_requires_exact_host_allowlist():
    with pytest.raises(ValueError, match="allowlist"):
        validate_server_url(
            "https://internal.example.com/mcp",
            allowed_hosts={"knowledge.example.com"},
        )


def test_mcp_client_requires_allowlisted_https_host():
    with pytest.raises(ValueError, match="allowlist"):
        MCPToolClient(
            server_url="https://unexpected.example.com/mcp",
            api_key="x" * 40,
            allowed_tools={"search_knowledge"},
            allowed_hosts={"knowledge.example.com"},
        )
