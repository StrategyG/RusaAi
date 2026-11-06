import asyncio

from rusa_agents.http_limits import BodyLimitMiddleware


def test_body_limit_rejects_chunked_request_without_content_length():
    messages = iter([
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        raise AssertionError("oversized request must be rejected before the application")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/agent/tasks",
        "raw_path": b"/v1/agent/tasks",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }
    middleware = BodyLimitMiddleware(app, max_bytes=lambda: 6)
    asyncio.run(middleware(scope, receive, send))

    response_start = next(item for item in sent if item["type"] == "http.response.start")
    assert response_start["status"] == 413


def test_body_limit_replays_complete_request_body():
    messages = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        body = await receive()
        assert body["body"] == b"123456"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/agent/tasks",
        "raw_path": b"/v1/agent/tasks",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }
    middleware = BodyLimitMiddleware(app, max_bytes=lambda: 6)
    asyncio.run(middleware(scope, receive, send))
    assert any(item.get("status") == 200 for item in sent)
