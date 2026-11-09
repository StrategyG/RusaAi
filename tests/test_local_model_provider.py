import asyncio
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")),
)

from app import LocalModelInferenceProvider, _model_timeout_seconds
from model_runtime import GenerationEvent, GenerationResult


class FakeBackend:
    async def generate(self, messages, *, max_new_tokens, temperature, top_p):
        assert messages == [{"role": "user", "content": "hello"}]
        assert max_new_tokens == 12
        assert temperature == 0.2
        assert top_p == 0.8
        return GenerationResult(
            content="hello world",
            prompt_tokens=3,
            completion_tokens=2,
            finish_reason="stop",
        )

    async def stream(self, messages, *, max_new_tokens, temperature, top_p):
        yield GenerationEvent(delta="hello ")
        yield GenerationEvent(delta="world")
        yield GenerationEvent(
            finish_reason="stop",
            prompt_tokens=3,
            completion_tokens=2,
        )

    async def close(self):
        self.closed = True


def test_local_provider_adapts_complete_contract():
    provider = LocalModelInferenceProvider(FakeBackend())
    result = asyncio.run(
        provider.complete(
            [{"role": "user", "content": "hello"}],
            max_tokens=12,
            temperature=0.2,
            top_p=0.8,
        )
    )
    assert result == "hello world"


def test_local_provider_adapts_stream_events():
    provider = LocalModelInferenceProvider(FakeBackend())

    async def collect():
        return [
            event
            async for event in provider.stream(
                [{"role": "user", "content": "hello"}],
                max_tokens=12,
                temperature=0.2,
                top_p=0.8,
            )
        ]

    events = asyncio.run(collect())
    assert [event.content for event in events] == ["hello ", "world", None]
    assert events[-1].finish_reason == "stop"
    assert events[-1].prompt_tokens == 3
    assert events[-1].completion_tokens == 2


def test_local_provider_readiness_and_close():
    backend = FakeBackend()
    provider = LocalModelInferenceProvider(backend)
    assert asyncio.run(provider.check_ready()) is True
    asyncio.run(provider.close())
    assert backend.closed is True


def test_model_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("RUSA_GENERATION_TIMEOUT_SECONDS", "300")
    monkeypatch.delenv("RUSA_MODEL_TIMEOUT_SECONDS", raising=False)
    assert _model_timeout_seconds() == 300

    monkeypatch.setenv("RUSA_MODEL_TIMEOUT_SECONDS", "0")
    with pytest.raises(RuntimeError, match="between 1 and 3600"):
        _model_timeout_seconds()