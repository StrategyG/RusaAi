import json

import httpx
import pytest

from rusa_agents.external_rag import ExternalRAGClient, ExternalRAGError, validate_endpoint


def evidence():
    return {
        "id": "chunk-1",
        "text": "Evidence text with source context.",
        "source": {
            "uri": "https://docs.example.org/guide",
            "title": "Guide",
            "revision": "rev-17",
            "content_sha256": "a" * 64,
            "locator": "section:3",
        },
        "score": 0.73,
        "metadata": {"language": "en"},
    }


def test_endpoint_requires_allowlisted_https_host():
    assert validate_endpoint(
        "https://rag.example.org/v1/retrieve", {"rag.example.org"}
    ).endswith("/v1/retrieve")
    with pytest.raises(ValueError):
        validate_endpoint("https://unknown.example.org/v1/retrieve", {"rag.example.org"})
    with pytest.raises(ValueError):
        validate_endpoint("http://rag.example.org/v1/retrieve", {"rag.example.org"})


@pytest.mark.asyncio
async def test_search_validates_and_returns_provenance():
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + ("s" * 40)
        body = json.loads(request.content)
        assert body["schema_version"] == "1.0"
        assert body["top_k"] == 3
        return httpx.Response(200, json={"schema_version": "1.0", "results": [evidence()]})

    client = ExternalRAGClient(
        endpoint="https://rag.example.org/v1/retrieve",
        bearer_token="s" * 40,
        allowed_hosts={"rag.example.org"},
        transport=httpx.MockTransport(handler),
    )
    results = await client.search("find evidence", top_k=3)
    assert len(results) == 1
    assert results[0].source_uri == "https://docs.example.org/guide"
    assert results[0].content_sha256 == "a" * 64


@pytest.mark.asyncio
async def test_search_rejects_unprovenanced_evidence():
    def handler(request):
        return httpx.Response(200, json={"schema_version": "1.0", "results": [{"id": "x", "text": "bad", "source": {}}]})

    client = ExternalRAGClient(
        endpoint="https://rag.example.org/v1/retrieve",
        bearer_token="s" * 40,
        allowed_hosts={"rag.example.org"},
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ExternalRAGError):
        await client.search("find evidence")


@pytest.mark.asyncio
async def test_search_deduplicates_same_id_and_hash():
    def handler(request):
        item = evidence()
        return httpx.Response(200, json={"schema_version": "1.0", "results": [item, item]})

    client = ExternalRAGClient(
        endpoint="https://rag.example.org/v1/retrieve",
        bearer_token="s" * 40,
        allowed_hosts={"rag.example.org"},
        transport=httpx.MockTransport(handler),
    )
    results = await client.search("find evidence", top_k=2)
    assert len(results) == 1
