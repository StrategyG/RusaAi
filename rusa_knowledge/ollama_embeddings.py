"""Bounded local Ollama embedding provider for air-gapped and private RAG deployments."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
from urllib.parse import urlsplit

import httpx

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaEmbeddingProvider:
    """Call Ollama's local /api/embed endpoint and pad vectors without changing cosine geometry."""

    provider_name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "bge-m3",
        native_dimensions: int = 1024,
        output_dimensions: int = 1536,
        timeout_seconds: float = 60.0,
        allow_http_hosts: set[str] | frozenset[str] = frozenset({"ollama"}),
        max_response_bytes: int = 8 * 1024 * 1024,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        trusted_hosts = {host.strip().lower() for host in allow_http_hosts}
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host) for host in trusted_hosts):
            raise ValueError("local embedding HTTP host allowlist is invalid")
        secure = parsed.scheme == "https"
        local_http = (
            parsed.scheme == "http"
            and bool(parsed.hostname)
            and (
                _is_loopback(parsed.hostname)
                or parsed.hostname.lower() in trusted_hosts
            )
        )
        if (
            not (secure or local_http)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Ollama URL must use HTTPS or an explicitly trusted local HTTP host")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("embedding model name is invalid")
        if not 1 <= native_dimensions <= output_dimensions <= 8192:
            raise ValueError("embedding dimensions must satisfy 1 <= native <= output <= 8192")
        if timeout_seconds <= 0 or max_response_bytes < 1024:
            raise ValueError("embedding transport limits are invalid")
        if isinstance(max_retries, bool) or not 0 <= max_retries <= 5:
            raise ValueError("embedding retries must be between 0 and 5")
        if not 0 <= retry_backoff_seconds <= 5:
            raise ValueError("embedding retry backoff must be between 0 and 5 seconds")

        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/api/embed"
        self.tags_endpoint = f"{self.base_url}/api/tags"
        self.model = model.strip()
        self.native_dimensions = native_dimensions
        self.output_dimensions = output_dimensions
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def _request_json(self, method: str, url: str, payload: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            retry_delay = 0.0
            try:
                with self._client.stream(method, url, json=payload) as response:
                    if response.status_code in _RETRYABLE_STATUS:
                        raw_retry = response.headers.get("retry-after", "")
                        try:
                            retry_delay = min(2.0, max(0.0, float(raw_retry)))
                        except ValueError:
                            retry_delay = 0.0
                        status = response.status_code
                    elif response.is_error:
                        raise RuntimeError(
                            f"local embedding service returned HTTP {response.status_code}"
                        )
                    else:
                        declared_length = response.headers.get("content-length")
                        if declared_length is not None:
                            try:
                                if int(declared_length) > self.max_response_bytes:
                                    raise RuntimeError("embedding response exceeds size limit")
                            except ValueError as exc:
                                raise RuntimeError("embedding service returned invalid content length") from exc
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=64 * 1024):
                            if len(body) + len(chunk) > self.max_response_bytes:
                                raise RuntimeError("embedding response exceeds size limit")
                            body.extend(chunk)
                        try:
                            decoded = json.loads(body)
                        except (ValueError, TypeError) as exc:
                            raise RuntimeError("local embedding service returned invalid JSON") from exc
                        if not isinstance(decoded, dict):
                            raise RuntimeError("local embedding service returned an invalid object")
                        return decoded
                if attempt == self.max_retries:
                    raise RuntimeError(
                        f"local embedding service remained unavailable (HTTP {status})"
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    raise RuntimeError("local embedding service is unavailable") from exc
            if attempt < self.max_retries:
                delay = max(retry_delay, self.retry_backoff_seconds * (2 ** attempt))
                if delay:
                    time.sleep(min(delay, 2.0))
        raise RuntimeError("local embedding service is unavailable") from last_error

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not 1 <= len(texts) <= 64:
            raise ValueError("embedding batch must contain between 1 and 64 texts")
        if any(
            not isinstance(text, str) or not text.strip() or len(text) > 20_000
            for text in texts
        ):
            raise ValueError("each embedding input must contain 1 to 20000 characters")

        response = self._request_json(
            "POST",
            self.endpoint,
            {"model": self.model, "input": texts, "truncate": False, "keep_alive": "10m"},
        )
        vectors = response.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError("Ollama returned an unexpected number of embedding vectors")

        result: list[list[float]] = []
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.native_dimensions:
                raise RuntimeError("Ollama embedding dimension does not match configuration")
            try:
                values = [float(value) for value in vector]
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Ollama embedding contains a non-numeric value") from exc
            if not all(math.isfinite(value) and abs(value) <= 1_000_000 for value in values):
                raise RuntimeError("Ollama embedding contains invalid or out-of-range values")
            norm_squared = sum(value * value for value in values)
            if not math.isfinite(norm_squared) or norm_squared <= 0:
                raise RuntimeError("Ollama embedding must have a finite, non-zero norm")
            # Appending zero coordinates preserves dot products, vector norms, and cosine scores.
            result.append(values + [0.0] * (self.output_dimensions - self.native_dimensions))
        return result

    def check_ready(self) -> bool:
        try:
            response = self._request_json("GET", self.tags_endpoint)
        except Exception:
            return False
        models = response.get("models")
        if not isinstance(models, list):
            return False
        return any(
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and (
                item["name"] == self.model
                or item["name"].startswith(self.model + ":")
            )
            for item in models
        )

    def close(self) -> None:
        self._client.close()
