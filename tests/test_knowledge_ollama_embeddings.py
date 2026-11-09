import json

import httpx
import pytest

from rusa_knowledge.ollama_embeddings import OllamaEmbeddingProvider


def test_ollama_provider_pads_vectors_without_changing_cosine_geometry():
    provider = OllamaEmbeddingProvider(
        base_url="http://ollama:11434",
        model="bge-m3",
        native_dimensions=3,
        output_dimensions=5,
        allow_http_hosts={"ollama"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"embeddings": [[3.0, 4.0, 0.0], [0.0, 2.0, 0.0]]},
            )
        ),
    )
    try:
        vectors = provider.embed(["first", "second"])
        assert vectors == [
            [3.0, 4.0, 0.0, 0.0, 0.0],
            [0.0, 2.0, 0.0, 0.0, 0.0],
        ]
    finally:
        provider.close()


def test_ollama_provider_retries_transient_http_errors():
    calls = {"count": 0}

    def transport(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0, 0.0]]})

    provider = OllamaEmbeddingProvider(
        base_url="http://localhost:11434",
        native_dimensions=3,
        output_dimensions=3,
        max_retries=1,
        retry_backoff_seconds=0,
        transport=httpx.MockTransport(transport),
    )
    try:
        assert provider.embed(["query"]) == [[1.0, 0.0, 0.0]]
        assert calls["count"] == 2
    finally:
        provider.close()


def test_ollama_provider_rejects_untrusted_plain_http_and_bad_dimensions():
    with pytest.raises(ValueError, match="trusted local HTTP"):
        OllamaEmbeddingProvider(base_url="http://example.com:11434")
    provider = OllamaEmbeddingProvider(
        base_url="http://localhost:11434",
        native_dimensions=3,
        output_dimensions=4,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"embeddings": [[1.0, 2.0]]})
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="dimension"):
            provider.embed(["query"])
    finally:
        provider.close()


def test_ollama_readiness_requires_the_configured_model():
    provider = OllamaEmbeddingProvider(
        base_url="http://localhost:11434",
        model="bge-m3",
        native_dimensions=3,
        output_dimensions=3,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"models": [{"name": "bge-m3:latest"}]},
            )
        ),
    )
    try:
        assert provider.check_ready() is True
    finally:
        provider.close()
