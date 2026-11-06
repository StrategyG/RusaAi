import asyncio

import pytest

from rusa_agents.http_limits import BodyLimitMiddleware


def run_body(chunks, limit=8, headers=(), disconnect=False):
    sent, received = [], []
    messages = iter([
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1 or disconnect}
        for i, chunk in enumerate(chunks)
    ] + ([{"type": "http.disconnect"}] if disconnect else []))

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        received.append((await receive())["body"])

    asyncio.run(BodyLimitMiddleware(app, lambda: limit)(
        {"type": "http", "headers": headers}, receive, send,
    ))
    return sent, received


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")]])
def test_chunked_or_underreported_body_rejected_before_app(headers):
    sent, received = run_body([b"12345", b"6789"], headers=headers)
    assert sent[0]["status"] == 413
    assert received == []


def test_exact_limit_replayed_without_data_loss():
    sent, received = run_body([b"1234", b"5678"])
    assert sent == []
    assert received == [b"12345678"]


def test_disconnect_does_not_invoke_application():
    assert run_body([b"123"], disconnect=True) == ([], [])


def test_slow_request_times_out_before_application():
    sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    async def app(*args):
        pytest.fail("Incomplete request must not reach application")

    asyncio.run(BodyLimitMiddleware(app, lambda: 8, timeout=0.01)(
        {"type": "http", "headers": []}, receive, send,
    ))
    assert sent[0]["status"] == 408
