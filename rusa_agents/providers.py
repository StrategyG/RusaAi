"""Provider adapter for OpenAI-compatible model endpoints."""

from __future__ import annotations

from typing import Any

import httpx


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("provider base URL must use HTTP or HTTPS")
        if not model.strip():
            raise ValueError("provider model must not be empty")
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
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint, headers=headers, json=body)
            response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("model provider returned no completion choices")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model provider returned an empty completion")
        return content.strip()
