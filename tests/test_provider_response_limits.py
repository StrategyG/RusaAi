import asyncio

import httpx
import pytest

from rusa_agents.providers import (
    _decode_provider_json,
    _extract_chat_content,
    _read_bounded_body,
)


class ChunkedResponseStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"1234"
        yield b"5678"

    async def aclose(self):
        return None


def test_provider_response_rejects_oversized_declared_length():
    response = httpx.Response(200, headers={"content-length": "9"}, content=b"123456789")
    with pytest.raises(RuntimeError, match="size limit"):
        asyncio.run(_read_bounded_body(response, 8))


def test_provider_response_rejects_chunked_overflow_without_content_length():
    response = httpx.Response(200, stream=ChunkedResponseStream())
    with pytest.raises(RuntimeError, match="size limit"):
        asyncio.run(_read_bounded_body(response, 7))


def test_provider_response_accepts_bounded_json_and_extracts_content():
    raw = b'{"choices":[{"message":{"content":"ready"}}]}'
    response = httpx.Response(200, content=raw)
    body = asyncio.run(_read_bounded_body(response, 128))
    assert _extract_chat_content(_decode_provider_json(body)) == "ready"


def test_provider_response_rejects_malformed_chat_shape():
    with pytest.raises(RuntimeError, match="missing chat content"):
        _extract_chat_content({"choices": []})


def test_provider_response_rejects_invalid_json():
    with pytest.raises(RuntimeError, match="invalid JSON"):
        _decode_provider_json(b"{not-json")
