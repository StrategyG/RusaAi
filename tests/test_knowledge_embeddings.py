import json

import httpx
import pytest

from rusa_knowledge.embeddings import OpenAICompatibleEmbeddingProvider


def _response(request):
    payload = json.loads(request.content)
    vectors = [
        {"index": index, "embedding": [float(index + 1), 1.0, 2.0, 3.0]}
        for index, _ in enumerate(payload["input"])
    ]
    return httpx.Response(200, json={"data": vectors})


def test_embedding_provider_validates_and_orders_vectors():
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:9000/v1",
        model="test-embed",
        dimensions=4,
        transport=httpx.MockTransport(_response),
    )
    assert provider.embed(["first", "second"]) == [
        [1.0, 1.0, 2.0, 3.0],
        [2.0, 1.0, 2.0, 3.0],
    ]


def test_embedding_provider_rejects_remote_http():
    with pytest.raises(ValueError):
        OpenAICompatibleEmbeddingProvider(
            base_url="http://embedding.example/v1",
            dimensions=4,
        )


def test_embedding_provider_rejects_wrong_dimensions():
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://localhost:9000/v1",
        dimensions=4,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
            )
        ),
    )
    with pytest.raises(RuntimeError, match="dimension"):
        provider.embed(["query"])
