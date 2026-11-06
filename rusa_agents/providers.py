"""Configurable OpenAI-compatible inference provider; no vendor is hard-coded."""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
    ):
        self.base_url = (base_url or os.getenv("RUSA_MODEL_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("RUSA_MODEL_NAME", "rusa-ai")
        self.api_key = api_key or os.getenv("RUSA_MODEL_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
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
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue
                    response.raise_for_status()
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        raise RuntimeError("Model provider returned an empty or non-text response")
                    return content
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt >= self.max_retries:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError("Model provider request failed")
