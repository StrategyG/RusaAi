import asyncio

from rusa_agents.http_limits import BodyLimitMiddleware


def run_middleware(messages, max_bytes, timeout=1.0, receive_delay=0.0):
    sent = []
    observed = {}

    async def inner(scope, receive, send):
        message = await receive()
        observed["body"] = message["body"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        if receive_delay:
            await asyncio.sleep(receive_delay)
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 80),
    }
    middleware = BodyLimitMiddleware(
        inner,
        max_bytes=lambda: max_bytes,
        timeout=timeout,
    )
    asyncio.run(middleware(scope, receive, send))
    return sent, observed


def test_body_limit_rejects_chunked_request_before_app():
    sent, observed = run_middleware(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ],
        max_bytes=7,
    )
    assert sent[0]["status"] == 413
    assert "body" not in observed


def test_body_limit_replays_bounded_body_to_application():
    sent, observed = run_middleware(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ],
        max_bytes=8,
    )
    assert sent[0]["status"] == 200
    assert observed["body"] == b"123456"


def test_body_limit_times_out_slow_request_body():
    sent, observed = run_middleware(
        [{"type": "http.request", "body": b"slow", "more_body": False}],
        max_bytes=32,
        timeout=0.001,
        receive_delay=0.05,
    )
    assert sent[0]["status"] == 408
    assert "body" not in observed
