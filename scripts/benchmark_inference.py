#!/usr/bin/env python3
"""Reproducible local RUSA-AI latency/throughput benchmark.

Requires a real checkpoint configured through RUSA_MODEL_PATH. No network calls
are made and prompts/responses are not written to the report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from typing import Any, Dict, List

from inference import ModelRuntime


PROMPTS = [
    "Explain the difference between authentication and authorization in two paragraphs.",
    "Write a concise Python function that validates an email address and explain its limits.",
    "Summarize the main steps in a safe software deployment checklist.",
    "Describe how vector search and keyword search can complement one another.",
    "Explain why reproducible performance benchmarks need a fixed workload.",
]


def percentile(values: List[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((percent / 100) * (len(ordered) - 1))))
    return ordered[index]


def build_messages(prompt: str) -> List[Dict[str, str]]:
    return [{"role": "user", "content": prompt}]


async def benchmark_stream(
    runtime: ModelRuntime, prompts: List[str], max_tokens: int
) -> Dict[str, Any]:
    ttft_ms: List[float] = []
    e2e_ms: List[float] = []
    completion_tokens = 0

    for prompt in prompts:
        messages = build_messages(prompt)
        started = time.perf_counter()
        first_token_at = None
        chunks = []
        async for piece in runtime.stream(
            messages, temperature=0.0, top_p=1.0, max_tokens=max_tokens
        ):
            if first_token_at is None:
                first_token_at = time.perf_counter()
            chunks.append(piece)
        finished = time.perf_counter()
        ttft_ms.append(((first_token_at or finished) - started) * 1000)
        e2e_ms.append((finished - started) * 1000)
        completion_tokens += runtime.count_usage(messages, "".join(chunks))[
            "completion_tokens"
        ]

    total_seconds = sum(e2e_ms) / 1000
    return {
        "mode": "stream",
        "requests": len(prompts),
        "ttft_ms": {
            "p50": percentile(ttft_ms, 50),
            "p95": percentile(ttft_ms, 95),
            "p99": percentile(ttft_ms, 99),
        },
        "end_to_end_ms": {
            "p50": percentile(e2e_ms, 50),
            "p95": percentile(e2e_ms, 95),
            "p99": percentile(e2e_ms, 99),
        },
        "completion_tokens": completion_tokens,
        "tokens_per_second": completion_tokens / total_seconds if total_seconds else 0,
        "requests_per_second": len(prompts) / total_seconds if total_seconds else 0,
    }


def benchmark_batch(
    runtime: ModelRuntime,
    prompts: List[str],
    batch_size: int,
    max_tokens: int,
) -> Dict[str, Any]:
    batch_latencies_ms: List[float] = []
    completion_tokens = 0
    total_elapsed = 0.0
    completed_requests = 0

    for offset in range(0, len(prompts), batch_size):
        group = prompts[offset : offset + batch_size]
        messages = [build_messages(prompt) for prompt in group]
        started = time.perf_counter()
        if len(messages) == 1:
            results = [
                runtime.generate(
                    messages[0], temperature=0.0, top_p=1.0, max_tokens=max_tokens
                )
            ]
        else:
            results = runtime.generate_batch(
                messages, temperature=0.0, top_p=1.0, max_tokens=max_tokens
            )
        elapsed = time.perf_counter() - started
        elapsed_ms = elapsed * 1000
        batch_latencies_ms.append(elapsed_ms)
        total_elapsed += elapsed
        completed_requests += len(results)
        completion_tokens += sum(item["completion_tokens"] for item in results)

    return {
        "mode": "tensor_batch",
        "requests": completed_requests,
        "batch_size": batch_size,
        "batch_latency_ms": {
            "p50": percentile(batch_latencies_ms, 50),
            "p95": percentile(batch_latencies_ms, 95),
            "p99": percentile(batch_latencies_ms, 99),
        },
        "completion_tokens": completion_tokens,
        "tokens_per_second": completion_tokens / total_elapsed if total_elapsed else 0,
        "requests_per_second": completed_requests / total_elapsed if total_elapsed else 0,
        "total_elapsed_seconds": total_elapsed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    if args.requests < 1 or args.warmup < 0 or args.batch_size < 1:
        parser.error("requests and batch-size must be positive; warmup cannot be negative")
    if args.stream and args.batch_size != 1:
        parser.error("stream benchmark supports batch-size=1 only")

    runtime = ModelRuntime()
    runtime.load()
    try:
        warmup_messages = build_messages(PROMPTS[0])
        for _ in range(args.warmup):
            runtime.generate(
                warmup_messages, temperature=0.0, top_p=1.0, max_tokens=args.max_tokens
            )

        prompts = [PROMPTS[index % len(PROMPTS)] for index in range(args.requests)]
        if args.stream:
            result = asyncio.run(benchmark_stream(runtime, prompts, args.max_tokens))
        else:
            result = benchmark_batch(
                runtime, prompts, args.batch_size, args.max_tokens
            )

        report = {
            "model": runtime.model_name,
            "device": runtime.device,
            "checkpoint_path_configured": bool(os.getenv("RUSA_MODEL_PATH")),
            "warmup_requests": args.warmup,
            "max_tokens": args.max_tokens,
            "context_window": runtime.max_context_tokens,
            "result": result,
        }
        if runtime.device == "cuda":
            report["gpu_memory"] = {
                "peak_allocated_bytes": int(
                    runtime._torch.cuda.max_memory_allocated()
                ),
                "peak_reserved_bytes": int(
                    runtime._torch.cuda.max_memory_reserved()
                ),
            }
        print(json.dumps(report, indent=2))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
