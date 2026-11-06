"""Configurable OpenAI-compatible inference provider; no vendor is hard-coded."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx


async def _read_limited_json(
    response: httpx.Response,
    max_response_bytes: int,
) -> dict[str, Any]:
    declared_length = response.headers.get("content-length")
    if declared_length:
        try:
            if int(declared_length) > max_response_bytes:
                raise RuntimeError(
                    f"Model provider response exceeds the {max_response_bytes}-byte limit"
                )
        except ValueError as exc:
            raise RuntimeError("Model provider returned an invalid Content-Length") from exc

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > max_response_bytes:
            raise RuntimeError(
                f"Model provider response exceeds the {max_response_bytes}-byte limit"
            )
        body.extend(chunk)
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Model provider returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Model provider response must be a JSON object")
    return payload


def _response_limit(value: int | None) -> int:
    if value is None:
        try:
            value = int(os.getenv("RUSA_MODEL_MAX_RESPONSE_BYTES", "2000000"))
        except ValueError as exc:
            raise ValueError("RUSA_MODEL_MAX_RESPONSE_BYTES must be an integer") from exc
    if not 1 <= value <= 50_000_000:
        raise ValueError("Model response limit must be between 1 and 50000000 bytes")
    return value


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        max_response_bytes: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = (base_url or os.getenv("RUSA_MODEL_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("RUSA_MODEL_NAME", "rusa-ai")
        self.api_key = api_key or os.getenv("RUSA_MODEL_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_response_bytes = _response_limit(max_response_bytes)
        self.transport = transport
        if timeout_seconds <= 0 or max_retries < 0:
            raise ValueError("Provider timeout must be positive and max_retries cannot be negative")
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
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=self.transport
        ) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    retry = False
                    async with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                            retry = True
                        else:
                            response.raise_for_status()
                            body = await _read_limited_json(response, self.max_response_bytes)
                    if retry:
                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue
                    try:
                        content = body["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise RuntimeError("Model provider response is missing chat content") from exc
                    if not isinstance(content, str) or not content.strip():
                        raise RuntimeError("Model provider returned an empty or non-text response")
                    return content
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
        max_response_bytes: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = _response_limit(max_response_bytes)
        self.transport = transport
        if timeout_seconds <= 0:
            raise ValueError("Provider timeout must be positive")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def check_ready(self) -> bool:
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=self.transport
        ) as client:
            async with client.stream("GET", f"{self.base_url}/models", headers=self._headers()) as response:
                response.raise_for_status()
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
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=self.transport
        ) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                body = await _read_limited_json(response, self.max_response_bytes)
            try:
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError("Inference provider response is missing chat content") from exc
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("Inference provider returned empty or non-text content")
            return content
