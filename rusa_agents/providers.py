"""Secure OpenAI-compatible provider adapter with bounded responses."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("provider base URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote model provider endpoints must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("provider URL cannot contain credentials, query, or fragment")
        if not model.strip():
            raise ValueError("provider model must not be empty")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("provider timeout must be between 1 and 300 seconds")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key or ""
        self.timeout = httpx.Timeout(
            connect=5.0,
            read=timeout_seconds,
            write=30.0,
            pool=5.0,
        )

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "stream": False,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            async with client.stream("POST", self.endpoint, headers=headers, json=body) as response:
                response.raise_for_status()
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > 2_000_000:
                        raise RuntimeError("model provider response exceeded size limit")
                    chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("model provider returned no completion choices")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model provider returned an empty completion")
        return content.strip()
