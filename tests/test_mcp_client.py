import pytest

from rusa_mcp.client import MCPToolClient, validate_server_url


def test_mcp_url_requires_secure_transport_or_loopback():
    assert validate_server_url("https://knowledge.example.com/mcp")
    assert validate_server_url("http://127.0.0.1:8002/mcp")
    assert validate_server_url(
        "http://rusa-mcp:8002/mcp",
        allow_http_hosts={"rusa-mcp"},
    )
    with pytest.raises(ValueError):
        validate_server_url("http://knowledge.example.com/mcp")
    with pytest.raises(ValueError):
        validate_server_url("https://knowledge.example.com/other")


def test_mcp_client_requires_explicit_tool_allowlist():
    with pytest.raises(ValueError):
        MCPToolClient(
            server_url="https://knowledge.example.com/mcp",
            api_key="x" * 40,
            allowed_tools=set(),
        )


def test_mcp_client_requires_long_bearer_token():
    with pytest.raises(ValueError):
        MCPToolClient(
            server_url="https://knowledge.example.com/mcp",
            api_key="short",
            allowed_tools={"search_knowledge"},
        )
