#!/usr/bin/env python3
"""Bounded staging load test for the RUSA-AI chat-completions API."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from collections import Counter
from urllib.parse import urlparse

import httpx


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index] * 1000, 2)


async def run_load_test(args: argparse.Namespace) -> dict[str, object]:
    base_url = os.getenv("RUSA_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = os.getenv("RUSA_API_KEY", "")
    model = os.getenv("RUSA_MODEL_NAME", "rusa-ai")
    if not api_key:
        raise RuntimeError("RUSA_API_KEY is required")
    host = (urlparse(base_url).hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"} and not args.confirm_remote:
        raise RuntimeError("Remote load tests also require --confirm-remote")

    endpoint = f"{base_url}/v1/chat/completions"
    headers = {"X-API-Key": api_key}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": args.prompt}],
        "temperature": 0,
        "top_p": 1,
        "max_tokens": args.max_tokens,
        "stream": args.stream,
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    latencies: list[float] = []
    first_token_latencies: list[float] = []
    status_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    successes = 0
    started = time.perf_counter()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(args.timeout),
        limits=httpx.Limits(
            max_connections=args.concurrency,
            max_keepalive_connections=args.concurrency,
        ),
    ) as client:

        async def one_request() -> None:
            nonlocal successes
            async with semaphore:
                request_started = time.perf_counter()
                first_token_at: float | None = None
                try:
                    if args.stream:
                        async with client.stream(
                            "POST", endpoint, headers=headers, json=payload
                        ) as response:
                            status_counts[str(response.status_code)] += 1
                            if response.status_code != 200:
                                error_counts[f"http_{response.status_code}"] += 1
                                await response.aread()
                                return
                            total_bytes = 0
                            done = False
                            async for line in response.aiter_lines():
                                total_bytes += len(line.encode("utf-8"))
                                if total_bytes > 2_000_000:
                                    raise RuntimeError("stream_response_limit")
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    done = True
                                    break
                                event = json.loads(data)
                                if event.get("error") is not None:
                                    raise RuntimeError("provider_stream_error")
                                choices = event.get("choices") or []
                                if choices:
                                    delta = choices[0].get("delta") or {}
                                    if delta.get("content") and first_token_at is None:
                                        first_token_at = time.perf_counter()
                            if not done:
                                raise RuntimeError("stream_missing_done")
                    else:
                        response = await client.post(
                            endpoint, headers=headers, json=payload
                        )
                        status_counts[str(response.status_code)] += 1
                        if response.status_code != 200:
                            error_counts[f"http_{response.status_code}"] += 1
                            return
                        result = response.json()
                        content = result["choices"][0]["message"]["content"]
                        if not isinstance(content, str) or not content.strip():
                            raise RuntimeError("empty_completion")

                    latency = time.perf_counter() - request_started
                    latencies.append(latency)
                    if first_token_at is not None:
                        first_token_latencies.append(first_token_at - request_started)
                    successes += 1
                except Exception as exc:
                    error_counts[type(exc).__name__] += 1
                    status_counts["exception"] += 1

        await asyncio.gather(*(one_request() for _ in range(args.requests)))

    elapsed = time.perf_counter() - started
    failures = args.requests - successes
    return {
        "mode": "stream" if args.stream else "non_stream",
        "model": model,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "successes": successes,
        "failures": failures,
        "elapsed_seconds": round(elapsed, 3),
        "requests_per_second": round(successes / elapsed, 3) if elapsed else 0,
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "time_to_first_token_ms": {
            "p50": percentile(first_token_latencies, 0.50),
            "p95": percentile(first_token_latencies, 0.95),
        } if args.stream else None,
        "http_status_counts": dict(status_counts),
        "error_counts": dict(error_counts),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--prompt", default="Reply with a short RUSA-AI load-test acknowledgement.")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--confirm-remote", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("pass --confirm to acknowledge that this sends real inference requests")
    if not 1 <= args.requests <= 5000:
        parser.error("--requests must be between 1 and 5000")
    if not 1 <= args.concurrency <= 256:
        parser.error("--concurrency must be between 1 and 256")
    if not 1 <= args.max_tokens <= 4096:
        parser.error("--max-tokens must be between 1 and 4096")
    if not 1 <= args.timeout <= 900:
        parser.error("--timeout must be between 1 and 900 seconds")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run_load_test(args))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__},
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
