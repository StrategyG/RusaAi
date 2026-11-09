import asyncio
import json

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
        b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"
        for payload in payloads
    ) + b"data: [DONE]\n\n"

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
    body = b"data: " + b"x" * 128 + b"\n\n"
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
    body = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
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



def test_worker_provider_uses_cpu_generation_timeout_environment(monkeypatch):
    monkeypatch.setenv("RUSA_GENERATION_TIMEOUT_SECONDS", "300")
    monkeypatch.delenv("RUSA_MODEL_TIMEOUT_SECONDS", raising=False)
    provider = OpenAICompatibleProvider(
        base_url="http://llama-cpp:8080/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )
    assert provider.timeout_seconds == 300


def test_worker_provider_timeout_override_is_validated(monkeypatch):
    monkeypatch.setenv("RUSA_MODEL_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("RUSA_GENERATION_TIMEOUT_SECONDS", "300")
    provider = OpenAICompatibleProvider(base_url="http://llama-cpp:8080/v1")
    assert provider.timeout_seconds == 240

    monkeypatch.setenv("RUSA_MODEL_TIMEOUT_SECONDS", "invalid")
    with pytest.raises(ValueError, match="must be numeric"):
        OpenAICompatibleProvider(base_url="http://llama-cpp:8080/v1")


def test_llama_cpp_openai_compatible_endpoint():
    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "rusa-cpu"}]}, request=request)
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "rusa-cpu"
        assert payload["stream"] is False
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "RUSA-AI CPU ready"}}]},
            request=request,
        )

    provider = OpenAIChatInferenceProvider(
        base_url="http://llama-cpp:8080/v1",
        default_model="rusa-cpu",
        transport=httpx.MockTransport(handler),
    )

    async def verify():
        assert await provider.check_ready() is True
        return await provider.complete(
            [{"role": "user", "content": "health check"}],
            max_tokens=16,
        )

    assert asyncio.run(verify()) == "RUSA-AI CPU ready"

def test_inference_provider_uses_explicit_model_health_endpoint():
    requested_paths = []

    def handler(request):
        requested_paths.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    provider = OpenAIChatInferenceProvider(
        base_url="http://llama-cpp:8080/v1",
        readiness_url="http://llama-cpp:8080/health",
        transport=httpx.MockTransport(handler),
    )

    assert asyncio.run(provider.check_ready()) is True
    assert requested_paths == ["/health"]


def test_inference_provider_treats_model_loading_as_not_ready():
    provider = OpenAIChatInferenceProvider(
        base_url="http://llama-cpp:8080/v1",
        readiness_url="http://llama-cpp:8080/health",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"status": "loading"}, request=request)
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(provider.check_ready())