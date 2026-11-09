"""Hybrid OpenAI-compatible, local, and fail-closed mock provider chain."""
from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from prometheus_client import Counter, Histogram

logger = logging.getLogger("rusa-ai.code-refinery.providers")

LLM_REQUESTS = Counter(
    "rusa_code_refinery_llm_requests_total",
    "Code-refinery LLM requests by provider and outcome",
    ("provider", "outcome"),
)
LLM_DURATION = Histogram(
    "rusa_code_refinery_llm_request_duration_seconds",
    "Code-refinery LLM request duration by provider",
    ("provider",),
)


class CompletionProvider(Protocol):
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str: ...


class MockCodeProvider:
    """Deterministic fallback that blocks rather than fabricating implementation."""

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        return json.dumps(
            {
                "message": "All configured model providers are unavailable; mock mode will not generate code.",
                "actions": [],
                "done": True,
                "blocked": True,
                "blockers": ["No live LLM provider completed a valid structured response."],
            }
        )


class HybridCodeProvider:
    """Try the primary endpoint, then local inference, then a fail-closed mock."""

    def __init__(
        self,
        primary: CompletionProvider,
        local: CompletionProvider | None = None,
        mock: CompletionProvider | None = None,
    ) -> None:
        self.primary = primary
        self.local = local
        self.mock = mock or MockCodeProvider()

    @staticmethod
    def _validate_action_envelope(raw: str) -> None:
        payload = json.loads(raw)
        allowed = {"message", "actions", "done", "blocked", "blockers"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("LLM response must be a bounded JSON action envelope")
        if not isinstance(payload.get("message"), str):
            raise ValueError("LLM response message must be text")
        if not isinstance(payload.get("actions"), list) or not isinstance(payload.get("done"), bool):
            raise ValueError("LLM response actions/done fields are invalid")
        if "blocked" in payload and not isinstance(payload["blocked"], bool):
            raise ValueError("LLM response blocked field must be Boolean")
        blockers = payload.get("blockers", [])
        if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
            raise ValueError("LLM response blockers must be a string array")
        for action in payload["actions"]:
            if (
                not isinstance(action, dict)
                or set(action) - {"name", "arguments"}
                or not isinstance(action.get("name"), str)
                or not isinstance(action.get("arguments", {}), dict)
            ):
                raise ValueError("LLM response contains a malformed tool action")

    async def _attempt(
        self,
        name: str,
        provider: CompletionProvider,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        started = time.perf_counter()
        try:
            raw = await provider.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            self._validate_action_envelope(raw)
            LLM_REQUESTS.labels(name, "success").inc()
            return raw
        except Exception as exc:
            LLM_REQUESTS.labels(name, "failure").inc()
            logger.warning("Code-refinery %s provider failed (%s)", name, type(exc).__name__)
            raise
        finally:
            LLM_DURATION.labels(name).observe(time.perf_counter() - started)

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        failures: list[str] = []
        providers: list[tuple[str, CompletionProvider]] = [("primary", self.primary)]
        if self.local is not None:
            providers.append(("local", self.local))
        for name, provider in providers:
            try:
                return await self._attempt(name, provider, system_prompt, user_prompt)
            except Exception as exc:
                failures.append(f"{name}:{type(exc).__name__}")

        # The mock is a circuit-breaker response, never a code-generation success.
        LLM_REQUESTS.labels("mock", "blocked_fallback").inc()
        logger.error("All live code-refinery providers failed: %s", ",".join(failures))
        return await self.mock.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
