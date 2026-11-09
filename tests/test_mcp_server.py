import asyncio
import hashlib
import importlib
import json

import pytest

mcp = pytest.importorskip("mcp", reason="optional MCP runtime is not installed")
Client = mcp.Client
server = importlib.import_module("rusa_mcp.server")

class FakeKnowledgeService:
    def search(self, **kwargs):
        return [{
            "document_id": "00000000-0000-0000-0000-000000000001",
            "source_uri": "docs/guide.md",
            "content": "retrieved evidence",
            "score": 0.02,
        }]

    def get_document(self, **kwargs):
        return {"document_id": kwargs["document_id"], "content": "full document"}

    def list_collections(self, **kwargs):
        return [{"collection": "agent", "document_count": 1}]


def test_mcp_tools_require_request_tenant_and_return_grounded_results(monkeypatch):
    monkeypatch.setattr(server, "get_knowledge_service", lambda: FakeKnowledgeService())

    async def run():
        context_token = server.CURRENT_TENANT.set("tenant-a")
        try:
            async with Client(server.mcp) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {"search_knowledge", "get_document", "list_collections"} <= names
                result = await client.call_tool(
                    "search_knowledge",
                    {"query": "guide", "collection": "agent", "limit": 3},
                )
                assert result.is_error is False
                assert "retrieved evidence" in str(result.content)
        finally:
            server.CURRENT_TENANT.reset(context_token)

    asyncio.run(run())


def _invoke_middleware(middleware, headers):
    messages = []

    async def app(scope, receive, send):
        tenant = server.CURRENT_TENANT.get()
        body = json.dumps({"tenant": tenant}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }
    middleware.app = app
    asyncio.run(middleware(scope, receive, send))
    return messages


def test_mcp_auth_middleware_sets_tenant_from_hashed_bearer_token():
    token = "t" * 48
    digest = hashlib.sha256(token.encode()).hexdigest()
    middleware = server.TenantAuthMiddleware(
        lambda scope, receive, send: None,
        key_hashes={digest: "tenant-a"},
    )
    messages = _invoke_middleware(
        middleware,
        [(b"authorization", f"Bearer {token}".encode())],
    )
    assert messages[0]["status"] == 200
    assert json.loads(messages[1]["body"]) == {"tenant": "tenant-a"}


def test_mcp_auth_middleware_rejects_missing_or_invalid_bearer_token():
    middleware = server.TenantAuthMiddleware(
        lambda scope, receive, send: None,
        key_hashes={},
    )
    messages = _invoke_middleware(middleware, [])
    assert messages[0]["status"] == 401
    assert json.loads(messages[1]["body"]) == {"error": "unauthorized"}


def test_mcp_auth_rejects_duplicate_authorization_headers():
    token = "d" * 48
    digest = hashlib.sha256(token.encode()).hexdigest()
    middleware = server.TenantAuthMiddleware(
        lambda scope, receive, send: None,
        key_hashes={digest: "tenant-a"},
    )
    messages = _invoke_middleware(
        middleware,
        [
            (b"authorization", f"Bearer {token}".encode()),
            (b"authorization", f"Bearer {token}".encode()),
        ],
    )
    assert messages[0]["status"] == 401


def test_mcp_rate_limit_is_tenant_scoped_and_returns_retry_after():
    token = "r" * 48
    digest = hashlib.sha256(token.encode()).hexdigest()
    middleware = server.TenantAuthMiddleware(
        lambda scope, receive, send: None,
        key_hashes={digest: "tenant-a"},
        requests_per_minute=2,
        burst=2,
    )
    headers = [(b"authorization", f"Bearer {token}".encode())]
    assert _invoke_middleware(middleware, headers)[0]["status"] == 200
    assert _invoke_middleware(middleware, headers)[0]["status"] == 200
    limited = _invoke_middleware(middleware, headers)
    assert limited[0]["status"] == 429
    assert (b"retry-after", b"30") in limited[0]["headers"] or any(
        key == b"retry-after" for key, _ in limited[0]["headers"]
    )
