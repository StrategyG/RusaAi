"""Configurable OpenAI-compatible inference provider; no vendor is hard-coded."""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class InferenceStreamEvent:
    """One bounded delta or terminal event from an OpenAI-compatible SSE stream."""

    content: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 1024,
    ) -> AsyncIterator[InferenceStreamEvent]:
        """Stream bounded OpenAI-compatible SSE deltas without buffering the full response."""
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
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
                declared_length = response.headers.get("content-length")
                if declared_length:
                    try:
                        if int(declared_length) > self.max_response_bytes:
                            raise RuntimeError(
                                f"Model provider stream exceeds the {self.max_response_bytes}-byte limit"
                            )
                    except ValueError as exc:
                        raise RuntimeError(
                            "Model provider returned an invalid Content-Length"
                        ) from exc
                response.raise_for_status()

                total_bytes = 0
                buffer = bytearray()
                data_lines: list[bytes] = []
                finished = False
                done = False

                async for chunk in response.aiter_bytes(chunk_size=4096):
                    total_bytes += len(chunk)
                    if total_bytes > self.max_response_bytes:
                        raise RuntimeError(
                            f"Model provider stream exceeds the {self.max_response_bytes}-byte limit"
                        )
                    buffer.extend(chunk)
                    if len(buffer) > 65536 and b"\\n" not in buffer:
                        raise RuntimeError("Model provider SSE line exceeds the 65536-byte limit")

                    while b"\\n" in buffer:
                        line, _, remainder = buffer.partition(b"\\n")
                        buffer = bytearray(remainder)
                        line = line.rstrip(b"\\r")
                        if line.startswith(b"data:"):
                            data_lines.append(line[5:].lstrip())
                            if sum(map(len, data_lines)) > 65536:
                                raise RuntimeError("Model provider SSE event exceeds the 65536-byte limit")
                            continue
                        if line or not data_lines:
                            continue

                        event_data = b"\\n".join(data_lines)
                        data_lines.clear()
                        if event_data == b"[DONE]":
                            done = True
                            break
                        event = self._decode_stream_event(event_data)
                        if event is not None:
                            if event.finish_reason is not None:
                                finished = True
                            yield event
                    if done:
                        break

                if not done and (buffer or data_lines):
                    if buffer:
                        line = bytes(buffer).rstrip(b"\\r")
                        if line.startswith(b"data:"):
                            data_lines.append(line[5:].lstrip())
                    if data_lines:
                        event_data = b"\\n".join(data_lines)
                        if event_data == b"[DONE]":
                            done = True
                        else:
                            event = self._decode_stream_event(event_data)
                            if event is not None:
                                if event.finish_reason is not None:
                                    finished = True
                                yield event

                if not finished:
                    raise RuntimeError("Model provider stream ended without a finish reason")

    @staticmethod
    def _decode_stream_event(data: bytes) -> InferenceStreamEvent | None:
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Model provider returned malformed SSE JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Model provider SSE event must be a JSON object")
        if payload.get("error") is not None:
            raise RuntimeError("Model provider reported a streaming inference error")

        content: str | None = None
        finish_reason: str | None = None
        choices = payload.get("choices", [])
        if choices:
            if not isinstance(choices, list) or not isinstance(choices[0], dict):
                raise RuntimeError("Model provider returned an invalid stream choice")
            choice = choices[0]
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise RuntimeError("Model provider returned an invalid stream delta")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise RuntimeError("Model provider stream content must be text")
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise RuntimeError("Model provider finish_reason must be text")

        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            raise RuntimeError("Model provider returned invalid stream usage")
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        for name, value in (
            ("prompt_tokens", prompt_tokens),
            ("completion_tokens", completion_tokens),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise RuntimeError(f"Model provider returned invalid {name}")

        if (
            content is None
            and finish_reason is None
            and prompt_tokens is None
            and completion_tokens is None
        ):
            return None
        return InferenceStreamEvent(
            content=content,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

