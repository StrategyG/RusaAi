"""Model-provider boundary. No fake completions are returned."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx


class ModelProviderError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.getenv("RUSA_AGENT_MODEL_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("RUSA_AGENT_MODEL_API_KEY", "")
        self.model = model or os.getenv("RUSA_AGENT_MODEL_NAME", "")
        try:
            self.max_tokens = int(os.getenv("RUSA_AGENT_MODEL_MAX_TOKENS", "2048"))
        except ValueError as exc:
            raise ModelProviderError("RUSA_AGENT_MODEL_MAX_TOKENS must be an integer") from exc
        if not 256 <= self.max_tokens <= 4096:
            raise ModelProviderError("RUSA_AGENT_MODEL_MAX_TOKENS must be between 256 and 4096")
        if not self.base_url or not self.model:
            raise ModelProviderError("RUSA_AGENT_MODEL_BASE_URL and RUSA_AGENT_MODEL_NAME are required")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme != "https"
                and parsed.hostname not in {"localhost", "127.0.0.1"}
                and "." in (parsed.hostname or "")
            )
        ):
            raise ModelProviderError(
                "Model endpoint must use HTTPS for remote hosts and cannot embed URL credentials"
            )

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "top_p": 0.9,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
                response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
                data: dict[str, Any] = response.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ModelProviderError("Model provider returned empty content")
                return content.strip()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelProviderError("Model provider request failed or returned an invalid response") from exc
