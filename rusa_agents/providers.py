"""Configurable OpenAI-compatible inference provider; no vendor is hard-coded."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx


DEFAULT_MAX_PROVIDER_RESPONSE_BYTES = 4_000_000


async def _read_bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a provider response without allowing unbounded or decompressed payloads."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > max_bytes:
                raise RuntimeError("Model provider response exceeds the configured size limit")
        except ValueError as exc:
            raise RuntimeError("Model provider returned an invalid Content-Length header") from exc

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise RuntimeError("Model provider response exceeds the configured size limit")
    return bytes(body)


def _decode_provider_json(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Model provider returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise RuntimeError("Model provider returned an invalid response object")
    return body


def _extract_chat_content(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Model provider response is missing chat content") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Model provider returned an empty or non-text response")
    return content


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        max_response_bytes: int = DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
    ):
        self.base_url = (base_url or os.getenv("RUSA_MODEL_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("RUSA_MODEL_NAME", "rusa-ai")
        self.api_key = api_key or os.getenv("RUSA_MODEL_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes
        if timeout_seconds <= 0 or max_retries < 0 or max_response_bytes < 1:
            raise ValueError("Provider timeout, retry count, and response size must be positive")
        if not self.base_url:
            raise ValueError("RUSA_MODEL_BASE_URL must point to an OpenAI-compatible chat endpoint")

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }
        timeout = httpx.Timeout(self.timeout_seconds, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                            await asyncio.sleep(min(2 ** attempt, 8))
                            continue
                        response.raise_for_status()
                        raw = await _read_bounded_body(response, self.max_response_bytes)
                    return _extract_chat_content(_decode_provider_json(raw))
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt >= self.max_retries:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError("Model provider request failed")


class OpenAIChatInferenceProvider:
    """OpenAI-compatible chat adapter for the public inference endpoint."""

    def __init__(
        self,
        base_url: str,
        default_model: str = "rusa-ai",
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_response_bytes: int = DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        if not self.base_url or timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("Inference provider URL, timeout, and response size must be valid")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def check_ready(self) -> bool:
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", f"{self.base_url}/models", headers=self._headers()) as response:
                response.raise_for_status()
                await _read_bounded_body(response, min(self.max_response_bytes, 1_000_000))
            return True

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 1024,
    ) -> str:
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": False,
        }
        timeout = httpx.Timeout(self.timeout_seconds, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions",
                headers=self._headers(), json=payload,
            ) as response:
                response.raise_for_status()
                raw = await _read_bounded_body(response, self.max_response_bytes)
            return _extract_chat_content(_decode_provider_json(raw))
