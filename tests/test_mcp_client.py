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


@pytest.mark.parametrize(
    ("timeout_seconds", "max_argument_bytes", "max_response_bytes"),
    [
        (61, 32768, 1000000),
        (15, 1000001, 1000000),
        (15, 32768, 5000001),
    ],
)
def test_mcp_client_rejects_unbounded_limits(
    timeout_seconds, max_argument_bytes, max_response_bytes
):
    with pytest.raises(ValueError, match="bounds"):
        MCPToolClient(
            server_url="https://knowledge.example.com/mcp",
            api_key="x" * 40,
            allowed_tools={"search_knowledge"},
            allowed_hosts={"knowledge.example.com"},
            timeout_seconds=timeout_seconds,
            max_argument_bytes=max_argument_bytes,
            max_response_bytes=max_response_bytes,
        )
