#!/usr/bin/env python3
"""Bounded, dependency-free HTTP smoke latency probe for local/staging endpoints.

Not a production load tester. Does not measure inference tokens/sec.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; stable for small samples."""
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def probe(url: str, timeout: float) -> tuple[float, int]:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(4096)
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        status = 0
    return (time.perf_counter() - start) * 1000.0, status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/health/live")
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--allow-remote", action="store_true",
                        help="Explicitly allow non-loopback targets (staging only).")
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        parser.error("url must be an HTTP(S) URL")
    if not args.allow_remote and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("remote targets require --allow-remote; never use on production")
    if not 1 <= args.requests <= 1000 or not 1 <= args.concurrency <= 32:
        parser.error("requests must be 1..1000 and concurrency 1..32")
    if not 0 < args.timeout <= 30:
        parser.error("timeout must be >0 and <=30 seconds")
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda _: probe(args.url, args.timeout), range(args.requests)))
    elapsed = max(time.perf_counter() - start, 1e-9)
    latencies = [latency for latency, _ in results]
    failures = sum(status < 200 or status >= 400 for _, status in results)
    report = {
        "url": args.url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "elapsed_seconds": round(elapsed, 4),
        "throughput_rps": round(args.requests / elapsed, 3),
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 3),
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "p99": round(percentile(latencies, 0.99), 3),
        },
        "failures": failures,
        "error_rate": round(failures / args.requests, 6),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
