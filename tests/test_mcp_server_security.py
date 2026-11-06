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

    assert key_hashes == {digest: "tenant-a"}
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
