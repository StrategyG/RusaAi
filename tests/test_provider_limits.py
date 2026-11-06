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
