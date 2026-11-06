"""OpenAI-compatible provider adapter with bounded streaming responses."""

import ipaddress
import json
from urllib.parse import urlparse

import httpx


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 45.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        parsed = urlparse(base_url)
        secure = parsed.scheme == "https"
        local = (
            parsed.scheme == "http"
            and bool(parsed.hostname)
            and _is_loopback_host(parsed.hostname)
        )
        if (
            not (secure or local)
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("provider URL must use HTTPS or loopback HTTP without userinfo/query")
        if not api_key or not model:
            raise ValueError("provider API key and model are required")
        if timeout_seconds <= 0 or max_response_bytes < 1024:
            raise ValueError("invalid provider limits")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self.max_response_bytes = max_response_bytes

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(
                        f"model provider returned HTTP {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.max_response_bytes:
                        raise RuntimeError(
                            "model provider response exceeds the configured size limit"
                        )
                    body.extend(chunk)
        try:
            envelope = json.loads(body)
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("model provider returned an invalid completion envelope") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model provider returned an empty completion")
        return content
