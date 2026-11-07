import asyncio

import httpx

from scripts.benchmark_inference import stream_request


def test_benchmark_does_not_report_missing_token_usage_as_zero():
    body = (
        b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
        b'data: [DONE]\n\n'
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=body,
        )
    )

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await stream_request(
                client,
                "https://model.example/v1/chat/completions",
                {},
                {"model": "test", "messages": []},
            )

    result = asyncio.run(run())
    assert result["completion_tokens"] is None
    assert result["prompt_tokens"] is None


def test_benchmark_preserves_server_reported_token_usage():
    body = (
        b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        b'data: [DONE]\n\n'
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=body,
        )
    )

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await stream_request(
                client,
                "https://model.example/v1/chat/completions",
                {},
                {"model": "test", "messages": []},
            )

    result = asyncio.run(run())
    assert result["completion_tokens"] == 2
    assert result["prompt_tokens"] == 3
