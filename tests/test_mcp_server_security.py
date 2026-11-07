import asyncio
import hashlib
import importlib
import json

import pytest

pytest.importorskip("mcp", reason="optional MCP runtime is not installed")
server = importlib.import_module("rusa_mcp.server")

def test_tenant_tokens_are_stored_as_hashes_and_resolve_to_one_tenant():
    token = "a-secure-test-token-that-is-at-least-32-characters"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    key_hashes = server.parse_tenant_key_hashes(json.dumps({digest: "tenant-a"}))

    assert key_hashes[digest].tenant_id == "tenant-a"
    assert key_hashes[digest].subject == "legacy:tenant-a"
    assert server._tenant_for_token(token, key_hashes) == "tenant-a"
    assert server._tenant_for_token("different-token", key_hashes) is None
    assert server._tenant_for_token("short", key_hashes) is None


def test_tenant_key_configuration_rejects_invalid_entries():
    for raw in (
        "not-json",
        json.dumps({"not-a-sha256": "tenant-a"}),
        json.dumps({"a" * 64: "tenant with spaces"}),
        json.dumps(["not", "an", "object"]),
    ):
        try:
            server.parse_tenant_key_hashes(raw)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid tenant key configuration was accepted")


def test_auth_middleware_rejects_missing_bearer_token():
    async def inner_app(scope, receive, send):
        raise AssertionError("unauthenticated request reached MCP app")

    middleware = server.TenantAuthMiddleware(inner_app, {})
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/mcp", "headers": []},
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 401
    assert json.loads(sent[1]["body"]) == {"error": "unauthorized"}


def test_auth_middleware_sets_and_resets_tenant_context_per_request():
    token = "another-secure-test-token-that-is-at-least-32-characters"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    observed = []

    async def inner_app(scope, receive, send):
        observed.append(server.CURRENT_TENANT.get())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = server.TenantAuthMiddleware(inner_app, {digest: "tenant-a"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            },
            receive,
            send,
        )
    )
    assert observed == ["tenant-a"]
    assert server.CURRENT_TENANT.get() is None


def test_health_liveness_does_not_require_database_or_tenant_secrets():
    async def inner_app(scope, receive, send):
        raise AssertionError("liveness should be handled by middleware")

    middleware = server.TenantAuthMiddleware(inner_app, {})
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "method": "GET", "path": "/health/live", "headers": []},
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"])["status"] == "ok"


def test_mcp_refuses_worker_or_unspecified_database_role(monkeypatch):
    monkeypatch.setenv("RUSA_KNOWLEDGE_DB_ROLE", "rusa_knowledge_worker")
    with pytest.raises(RuntimeError, match="rusa_knowledge_mcp_reader"):
        server.validate_mcp_database_role()
    monkeypatch.delenv("RUSA_KNOWLEDGE_DB_ROLE")
    with pytest.raises(RuntimeError, match="rusa_knowledge_mcp_reader"):
        server.validate_mcp_database_role()


def test_mcp_accepts_only_dedicated_read_only_role(monkeypatch):
    monkeypatch.setenv("RUSA_KNOWLEDGE_DB_ROLE", "rusa_knowledge_mcp_reader")
    server.validate_mcp_database_role()


def test_auth_middleware_rejects_duplicate_authorization_headers():
    async def inner_app(scope, receive, send):
        raise AssertionError("duplicate authorization headers must not reach MCP app")

    middleware = server.TenantAuthMiddleware(inner_app, {})
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [
                    (b"authorization", b"Bearer first-token"),
                    (b"authorization", b"Bearer second-token"),
                ],
            },
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 401


def test_auth_middleware_rejects_malformed_bearer_header():
    async def inner_app(scope, receive, send):
        raise AssertionError("malformed bearer header must not reach MCP app")

    middleware = server.TenantAuthMiddleware(inner_app, {})
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [(b"authorization", b"Bearer token with spaces")],
            },
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 401


def test_production_mcp_tokens_require_scope_and_expiration():
    token = "production-mcp-token-that-is-long-enough-for-testing"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    legacy = json.dumps({digest: "tenant-a"})
    with pytest.raises(RuntimeError, match="production MCP tokens require"):
        server.parse_tenant_key_hashes(legacy, environment="production")

    claims = {
        "tenant_id": "tenant-a",
        "subject": "tenant-a-agent",
        "scopes": ["knowledge:read"],
        "not_before": 1_800_000_000,
        "expires_at": 1_800_086_400,
    }
    registry = server.parse_tenant_key_hashes(
        json.dumps({digest: claims}), environment="production"
    )
    assert server._tenant_for_token(token, registry, now=1_800_000_000) == "tenant-a"
    assert server._tenant_for_token(token, registry, now=1_799_999_999) is None
    assert server._tenant_for_token(token, registry, now=1_800_086_400) is None
    assert server._has_active_tokens(registry, now=1_800_000_000) is True
    assert server._has_active_tokens(registry, now=1_800_086_400) is False


def test_mcp_token_rejects_revoked_and_overlong_credentials():
    token = "revoked-mcp-token-that-is-long-enough-for-testing"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    claims = {
        "tenant_id": "tenant-a",
        "subject": "tenant-a-agent",
        "scopes": ["knowledge:read"],
        "not_before": 1_800_000_000,
        "expires_at": 1_800_086_400,
        "revoked": True,
    }
    registry = server.parse_tenant_key_hashes(json.dumps({digest: claims}))
    assert server._tenant_for_token(token, registry, now=1_800_000_000) is None
    claims["revoked"] = False
    claims["expires_at"] = claims["not_before"] + 31 * 24 * 60 * 60
    with pytest.raises(RuntimeError, match="30 days"):
        server.parse_tenant_key_hashes(json.dumps({digest: claims}))


def test_mcp_tenant_rate_limit_returns_429(monkeypatch):
    token = "rate-limit-mcp-token-that-is-long-enough-for-testing"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    monkeypatch.setenv("RUSA_MCP_RATE_LIMIT_PER_MINUTE", "1")

    async def inner_app(scope, receive, send):
        raise AssertionError("rate-limited request must not reach MCP app")

    middleware = server.TenantAuthMiddleware(inner_app, {digest: "tenant-a"})
    asyncio.run(middleware._allow_tenant_request("tenant-a"))
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            },
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 429
    response_headers = dict(sent[0]["headers"])
    assert int(response_headers[b"retry-after"]) > 0


def test_mcp_rate_limit_configuration_is_bounded(monkeypatch):
    monkeypatch.setenv("RUSA_MCP_RATE_LIMIT_PER_MINUTE", "0")
    with pytest.raises(RuntimeError, match="between 1 and 10000"):
        server.TenantAuthMiddleware(lambda *args: None, {})


def test_production_mcp_requires_active_expiring_token(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="active, non-revoked token"):
        server.TenantAuthMiddleware(lambda *args: None, {})


def test_production_mcp_requires_explicit_non_wildcard_hosts(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("RUSA_MCP_ALLOWED_HOSTS", raising=False)
    with pytest.raises(RuntimeError, match="RUSA_MCP_ALLOWED_HOSTS is required"):
        server._allowed_hosts()

    monkeypatch.setenv("RUSA_MCP_ALLOWED_HOSTS", "*")
    with pytest.raises(RuntimeError, match="must not contain wildcards"):
        server._allowed_hosts()

    monkeypatch.setenv("RUSA_MCP_ALLOWED_HOSTS", "mcp.example.com,knowledge-mcp:8002")
    assert server._allowed_hosts() == ["mcp.example.com", "knowledge-mcp:8002"]


def test_mcp_rejects_non_ascii_and_whitespace_bearer_tokens():
    whitespace_token = "x" * 31 + " " + "y"
    whitespace_digest = hashlib.sha256(whitespace_token.encode("utf-8")).hexdigest()
    assert server._tenant_for_token(
        whitespace_token, {whitespace_digest: "tenant-a"}
    ) is None

    unicode_token = "x" * 31 + "é"
    unicode_digest = hashlib.sha256(unicode_token.encode("utf-8")).hexdigest()
    assert server._tenant_for_token(unicode_token, {unicode_digest: "tenant-a"}) is None
