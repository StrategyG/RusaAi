import asyncio

import httpx
import pytest

from rusa_agents.providers import OpenAIChatInferenceProvider, OpenAICompatibleProvider


def test_agent_provider_accepts_bounded_json_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"done": true}'}}]},
            request=request,
        )
    )
    provider = OpenAICompatibleProvider(
        base_url="https://model.example/v1",
        transport=transport,
        max_response_bytes=1024,
    )
    result = asyncio.run(provider.complete(system_prompt="system", user_prompt="task"))
    assert result == '{"done": true}'


def test_agent_provider_rejects_oversized_model_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 128, request=request)
    )
    provider = OpenAICompatibleProvider(
        base_url="https://model.example/v1",
        transport=transport,
        max_response_bytes=32,
    )
    with pytest.raises(RuntimeError, match="exceeds"):
        asyncio.run(provider.complete(system_prompt="system", user_prompt="task"))


def test_inference_provider_rejects_oversized_model_response():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 128, request=request)
    )
    provider = OpenAIChatInferenceProvider(
        base_url="https://model.example/v1",
        transport=transport,
        max_response_bytes=32,
    )
    with pytest.raises(RuntimeError, match="exceeds"):
        asyncio.run(
            provider.complete(
                [{"role": "user", "content": "hello"}],
                max_tokens=10,
            )
        )


def test_agent_provider_stream_parses_crlf_sse_and_finish_reason():
    body = (
        b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\r\n'
        b"\r\n"
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\r\n'
        b"\r\n"
        b"data: [DONE]\r\n\r\n"
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
            request=request,
        )
    )
    provider = OpenAICompatibleProvider(
        base_url="https://model.example/v1",
        transport=transport,
        max_response_bytes=4096,
    )

    async def collect_events():
        return [
            event
            async for event in provider.stream(
                [{"role": "user", "content": "hello"}]
            )
        ]

    events = asyncio.run(collect_events())
    assert [event.content for event in events] == ["hello", None]
    assert events[-1].finish_reason == "stop"

