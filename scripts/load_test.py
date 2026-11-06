#!/usr/bin/env python3
"""Concurrent HTTP load test for a running RUSA-AI API instance.

Reports latency, throughput, status counts and optional streaming time-to-first-token.
It never records prompts, responses, or API keys in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from typing import Any, Dict, List

import httpx


PROMPT = "Explain why production inference systems need bounded concurrency and observability."


def percentile(values: List[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((percent / 100) * (len(ordered) - 1))))
    return ordered[index]


async def run_request(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    headers: Dict[str, str],
    model: str,
    max_tokens: int,
    streaming: bool,
) -> Dict[str, Any]:
    async with semaphore:
        started = time.perf_counter()
        first_token_at = None
        completion_tokens = 0
        status_code = 0
        error = False
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": PROMPT}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": streaming,
        }
        try:
            if streaming:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    status_code = response.status_code
                    if status_code == 200:
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                item = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if item.get("error"):
                                error = True
                            choices = item.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                if delta.get("content") is not None:
                                    if first_token_at is None:
                                        first_token_at = time.perf_counter()
                            if item.get("usage"):
                                completion_tokens = int(
                                    item["usage"].get("completion_tokens", 0)
                                )
                    else:
                        error = True
                        await response.aread()
            else:
                response = await client.post(
                    url, headers=headers, json=payload
                )
                status_code = response.status_code
                if status_code == 200:
                    data = response.json()
                    completion_tokens = int(
                        data.get("usage", {}).get("completion_tokens", 0)
                    )
                else:
                    error = True
        except Exception:
            error = True

        finished = time.perf_counter()
        return {
            "status": status_code,
            "latency_ms": (finished - started) * 1000,
            "ttft_ms": (
                (first_token_at - started) * 1000
                if first_token_at is not None
                else None
            ),
            "completion_tokens": completion_tokens,
            "error": error,
        }


async def main_async(args: argparse.Namespace) -> None:
    api_key = os.getenv("RUSA_API_KEY", "")
    if not api_key:
        raise SystemExit("Set RUSA_API_KEY in the environment before running the load test.")

    url = args.base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    semaphore = asyncio.Semaphore(args.concurrency)
    timeout = httpx.Timeout(args.timeout, connect=10.0)
    limits = httpx.Limits(
        max_connections=args.concurrency + 4,
        max_keepalive_connections=args.concurrency + 4,
    )

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        results = await asyncio.gather(
            *[
                run_request(
                    client,
                    semaphore,
                    url,
                    headers,
                    args.model,
                    args.max_tokens,
                    args.stream,
                )
                for _ in range(args.requests)
            ]
        )
    total_seconds = time.perf_counter() - started

    latencies = [item["latency_ms"] for item in results]
    ttft = [item["ttft_ms"] for item in results if item["ttft_ms"] is not None]
    completion_tokens = sum(item["completion_tokens"] for item in results)
    statuses = Counter(str(item["status"]) for item in results)
    failures = sum(1 for item in results if item["error"])

    report = {
        "base_url": args.base_url,
        "mode": "stream" if args.stream else "non_stream",
        "requests": args.requests,
        "concurrency": args.concurrency,
        "max_tokens": args.max_tokens,
        "duration_seconds": total_seconds,
        "status_counts": dict(statuses),
        "failures": failures,
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
        "ttft_ms": {
            "p50": percentile(ttft, 50),
            "p95": percentile(ttft, 95),
            "p99": percentile(ttft, 99),
        } if args.stream else None,
        "completion_tokens": completion_tokens,
        "requests_per_second": args.requests / total_seconds if total_seconds else 0,
        "tokens_per_second": completion_tokens / total_seconds if total_seconds else 0,
    }
    print(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--model", default=os.getenv("RUSA_MODEL_NAME", "rusa-ai"))
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    if min(args.requests, args.concurrency, args.max_tokens) < 1:
        parser.error("requests, concurrency and max-tokens must be positive")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
