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

def test_inference_provider_streams_bounded_sse_deltas_and_usage():
    import json

    payloads = [
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = b"".join(
        b"data: " + json.dumps(payload).encode("utf-8") + b"\\n\\n"
        for payload in payloads
    ) + b"data: [DONE]\\n\\n"

    def handler(request):
        sent = json.loads(request.content)
        assert sent["stream"] is True
        assert sent["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
            request=request,
        )

    provider = OpenAIChatInferenceProvider(
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(handler),
        max_response_bytes=4096,
    )

    async def collect():
        return [
            event
            async for event in provider.stream(
                [{"role": "user", "content": "hello"}],
                max_tokens=16,
            )
        ]

    events = asyncio.run(collect())
    assert [event.content for event in events if event.content] == ["hello ", "world"]
    assert events[-2].finish_reason == "stop"
    assert events[-1].prompt_tokens == 3
    assert events[-1].completion_tokens == 2


def test_inference_provider_rejects_oversized_sse_stream():
    body = b"data: " + b"x" * 128 + b"\\n\\n"
    provider = OpenAIChatInferenceProvider(
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
                request=request,
            )
        ),
        max_response_bytes=32,
    )

    async def collect():
        return [
            event
            async for event in provider.stream(
                [{"role": "user", "content": "hello"}],
                max_tokens=16,
            )
        ]

    with pytest.raises(RuntimeError, match="exceeds"):
        asyncio.run(collect())


def test_inference_provider_rejects_stream_without_terminal_reason():
    body = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\\n\\n'
    provider = OpenAIChatInferenceProvider(
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
                request=request,
            )
        ),
    )

    async def collect():
        return [
            event
            async for event in provider.stream(
                [{"role": "user", "content": "hello"}],
                max_tokens=16,
            )
        ]

    with pytest.raises(RuntimeError, match="without a finish reason"):
        asyncio.run(collect())

