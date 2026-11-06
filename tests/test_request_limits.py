import asyncio

from app import MaxRequestBodySizeMiddleware


def invoke_middleware(chunks, headers=(), limit=8):
    messages = iter(chunks)
    sent = []
    received_by_app = []
    downstream_called = False

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    async def downstream(scope, app_receive, app_send):
        nonlocal downstream_called
        downstream_called = True
        request_message = await app_receive()
        received_by_app.append(request_message.get("body", b""))
        await app_send({"type": "http.response.start", "status": 200, "headers": []})
        await app_send({"type": "http.response.body", "body": b"ok"})

    middleware = MaxRequestBodySizeMiddleware(downstream, max_body_bytes=limit)
    scope = {"type": "http", "method": "POST", "path": "/test", "headers": list(headers)}
    asyncio.run(middleware(scope, receive, send))
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    return status, b"".join(received_by_app), downstream_called


def test_chunked_request_body_over_limit_is_rejected():
    status, body, downstream_called = invoke_middleware([
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56789", "more_body": False},
    ])
    assert status == 413
    assert body == b""
    assert downstream_called is False


def test_chunked_request_body_under_limit_is_replayed_exactly():
    status, body, downstream_called = invoke_middleware([
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678", "more_body": False},
    ])
    assert status == 200
    assert body == b"12345678"
    assert downstream_called is True


def test_declared_content_length_is_rejected_before_reading_body():
    status, body, downstream_called = invoke_middleware(
        [{"type": "http.request", "body": b"", "more_body": False}],
        headers=[(b"content-length", b"9")],
    )
    assert status == 413
    assert body == b""
    assert downstream_called is False


def test_invalid_content_length_is_rejected():
    status, body, downstream_called = invoke_middleware(
        [{"type": "http.request", "body": b"", "more_body": False}],
        headers=[(b"content-length", b"invalid")],
    )
    assert status == 400
    assert body == b""
    assert downstream_called is False
