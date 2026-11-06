#!/usr/bin/env python3
"""Run non-streaming and SSE smoke tests against the configured model endpoint."""

from __future__ import annotations

import asyncio
import json
import os

from rusa_agents.providers import OpenAIChatInferenceProvider


async def run_smoke_test() -> dict[str, object]:
    base_url = os.getenv("RUSA_MODEL_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("RUSA_MODEL_BASE_URL is required")
    model = os.getenv("RUSA_MODEL_NAME", "rusa-ai")
    timeout = float(os.getenv("RUSA_MODEL_TIMEOUT_SECONDS", "120"))
    provider = OpenAIChatInferenceProvider(
        base_url,
        default_model=model,
        api_key=os.getenv("RUSA_MODEL_API_KEY"),
        timeout_seconds=timeout,
    )

    if not await provider.check_ready():
        raise RuntimeError("Model endpoint readiness check failed")

    messages = [
        {
            "role": "user",
            "content": "Reply with a short acknowledgement that the RUSA-AI inference smoke test is working.",
        }
    ]
    completion = await provider.complete(
        messages,
        model=model,
        temperature=0,
        top_p=1,
        max_tokens=32,
    )
    if not completion.strip():
        raise RuntimeError("Non-streaming inference returned empty content")

    streamed: list[str] = []
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    async for event in provider.stream(
        messages,
        model=model,
        temperature=0,
        top_p=1,
        max_tokens=32,
    ):
        if event.content:
            streamed.append(event.content)
        if event.finish_reason is not None:
            finish_reason = event.finish_reason
        if event.prompt_tokens is not None:
            prompt_tokens = event.prompt_tokens
        if event.completion_tokens is not None:
            completion_tokens = event.completion_tokens

    stream_text = "".join(streamed)
    if not stream_text.strip():
        raise RuntimeError("Streaming inference returned empty content")
    if finish_reason not in {"stop", "length"}:
        raise RuntimeError("Streaming inference did not return a valid finish reason")

    return {
        "status": "passed",
        "model": model,
        "readiness": "passed",
        "non_streaming": {"status": "passed", "characters": len(completion)},
        "streaming": {
            "status": "passed",
            "characters": len(stream_text),
            "finish_reason": finish_reason,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def main() -> int:
    try:
        result = asyncio.run(run_smoke_test())
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__},
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
