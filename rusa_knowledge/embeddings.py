"""OpenAI-compatible embedding client with bounded I/O and strict vector validation."""

from __future__ import annotations

import ipaddress
import json
import math
from urllib.parse import urlsplit

import httpx


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str = "text-embedding-3-small",
        api_key: str = "",
        dimensions: int = 1536,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 8 * 1024 * 1024,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        secure = parsed.scheme == "https"
        local = parsed.scheme == "http" and bool(parsed.hostname) and _is_loopback(parsed.hostname)
        if (
            not (secure or local)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("embedding URL must use HTTPS or loopback HTTP without credentials")
        if not model or len(model) > 200:
            raise ValueError("embedding model name is required and must be bounded")
        if not 1 <= dimensions <= 8192:
            raise ValueError("embedding dimensions must be between 1 and 8192")
        if timeout_seconds <= 0 or max_response_bytes < 1024:
            raise ValueError("invalid embedding transport limits")
        self.endpoint = f"{base_url.rstrip('/')}/embeddings"
        self.model = model
        self.api_key = api_key
        self.dimensions = dimensions
        self.timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self.max_response_bytes = max_response_bytes
        self.transport = transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not 1 <= len(texts) <= 64:
            raise ValueError("embedding batch must contain between 1 and 64 texts")
        if any(not isinstance(text, str) or not text.strip() or len(text) > 20_000 for text in texts):
            raise ValueError("each embedding input must contain 1 to 20000 characters")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "input": texts}
        body = bytearray()
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            with client.stream("POST", self.endpoint, headers=headers, json=payload) as response:
                if response.is_error:
                    raise RuntimeError(f"embedding provider returned HTTP {response.status_code}")
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > self.max_response_bytes:
                            raise RuntimeError("embedding response exceeds the configured size limit")
                    except ValueError as exc:
                        raise RuntimeError("embedding provider returned an invalid content length") from exc
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if len(body) + len(chunk) > self.max_response_bytes:
                        raise RuntimeError("embedding response exceeds the configured size limit")
                    body.extend(chunk)

        try:
            payload = json.loads(body)
            items = payload["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("embedding provider returned an invalid response") from exc
        if not isinstance(items, list) or len(items) != len(texts):
            raise RuntimeError("embedding provider returned an unexpected number of vectors")

        ordered: list[list[float] | None] = [None] * len(texts)
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("embedding provider returned an invalid vector item")
            index = item.get("index")
            vector = item.get("embedding")
            if type(index) is not int or not 0 <= index < len(texts) or ordered[index] is not None:
                raise RuntimeError("embedding provider returned invalid vector indexes")
            if not isinstance(vector, list) or len(vector) != self.dimensions:
                raise RuntimeError("embedding dimension does not match the configured dimension")
            try:
                values = [float(value) for value in vector]
            except (TypeError, ValueError) as exc:
                raise RuntimeError("embedding vector contains a non-numeric value") from exc
            if not all(math.isfinite(value) and abs(value) <= 1_000_000 for value in values):
                raise RuntimeError("embedding vector contains an invalid or out-of-range value")
            norm_squared = sum(value * value for value in values)
            if not math.isfinite(norm_squared) or norm_squared <= 0:
                raise RuntimeError("embedding vector must have a finite, non-zero norm")
            ordered[index] = values

        if any(vector is None for vector in ordered):
            raise RuntimeError("embedding provider omitted one or more vectors")
        return [vector for vector in ordered if vector is not None]
