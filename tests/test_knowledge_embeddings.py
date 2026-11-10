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


def test_embedding_provider_sends_e5_query_and_passage_modes():
    requests = []

    def response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return _response(request)

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://models.example.test/openai/v1",
        model="rusa-embedding-small",
        dimensions=4,
        input_type_enabled=True,
        transport=httpx.MockTransport(response),
    )
    provider.embed(["search question"], input_type="query")
    provider.embed(["document passage"], input_type="passage")

    assert requests[0]["input_type"] == "query"
    assert requests[1]["input_type"] == "passage"
    assert requests[0]["model"] == "rusa-embedding-small"


def test_embedding_provider_does_not_send_nonstandard_input_type_when_disabled():
    requests = []

    def response(request):
        requests.append(json.loads(request.content))
        return _response(request)

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:9000/v1",
        dimensions=4,
        transport=httpx.MockTransport(response),
    )
    provider.embed(["ordinary OpenAI-compatible request"], input_type="passage")
    assert "input_type" not in requests[0]


def test_embedding_provider_rejects_invalid_input_type():
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:9000/v1",
        dimensions=4,
        input_type_enabled=True,
        transport=httpx.MockTransport(_response),
    )
    with pytest.raises(ValueError, match="input_type"):
        provider.embed(["text"], input_type="invalid")
