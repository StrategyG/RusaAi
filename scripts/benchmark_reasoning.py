#!/usr/bin/env python3
"""Run a deterministic reasoning benchmark against a local or staging RUSA-AI API.

Remote targets require --allow-remote-staging and --environment staging.
Production endpoints are deliberately unsupported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from rusa_agents.benchmarking import score_reasoning_cases

DEFAULT_DATASET = Path("llm-project/data/reasoning_smoke.jsonl")


def load_dataset(path: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"dataset row {line_number} must be a JSON object")
        if not all(key in item for key in ("id", "category", "prompt", "accepted_answers")):
            raise ValueError(f"dataset row {line_number} is missing required fields")
        if not isinstance(item["prompt"], str) or not item["prompt"].strip():
            raise ValueError(f"dataset row {line_number} has an empty prompt")
        cases.append(item)
    if not cases:
        raise ValueError("reasoning dataset must contain at least one case")
    return cases


def git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return os.getenv("GITHUB_SHA")


def request_answer(
    base_url: str,
    api_key: str,
    model: str,
    case: dict[str, object],
    timeout: float,
) -> tuple[str, str | None]:
    try:
        response = httpx.post(
            base_url.rstrip("/") + "/v1/chat/completions",
            headers={"X-API-Key": api_key},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Solve the task carefully. Follow the requested answer format exactly.",
                    },
                    {"role": "user", "content": str(case["prompt"])},
                ],
                "temperature": 0,
                "max_tokens": 128,
                "stream": False,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        answer = payload["choices"][0]["message"]["content"]
        if not isinstance(answer, str):
            raise ValueError("assistant content is not a string")
        return answer, None
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        return "", f"{type(exc).__name__}: {str(exc)[:300]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=os.getenv("RUSA_MODEL_NAME", "rusa-ai"))
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--min-accuracy", type=float, default=0.90)
    parser.add_argument("--environment", choices=("local", "staging"), default="local")
    parser.add_argument("--allow-remote-staging", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reasoning-benchmark-report.json"))
    args = parser.parse_args()

    if not 1 <= args.concurrency <= 32 or args.timeout <= 0:
        parser.error("concurrency must be 1..32 and timeout must be positive")
    if not 0 <= args.min_accuracy <= 1:
        parser.error("min-accuracy must be between 0 and 1")

    parsed = urlparse(args.base_url)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and parsed.hostname not in local_hosts:
        parser.error("non-local benchmark targets must use HTTPS")
    if parsed.hostname not in local_hosts:
        if not args.allow_remote_staging or args.environment != "staging":
            parser.error("remote targets require --allow-remote-staging --environment staging")

    api_key = os.getenv("RUSA_API_KEY")
    if not api_key:
        parser.error("RUSA_API_KEY must be set in the environment")

    cases = load_dataset(args.dataset)
    predictions: dict[str, str] = {}
    failures: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(
                request_answer,
                args.base_url,
                api_key,
                args.model,
                case,
                args.timeout,
            ): case
            for case in cases
        }
        for future in as_completed(futures):
            case = futures[future]
            answer, error = future.result()
            case_id = str(case["id"])
            predictions[case_id] = answer
            if error:
                failures.append({"id": case_id, "error": error})

    scoring = score_reasoning_cases(cases, predictions)
    dataset_digest = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    report = {
        "schema_version": 1,
        "benchmark_id": "RUSA-REASONING-SMOKE-001",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": "StrategyG/RusaAi",
        "commit_sha": git_revision(),
        "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "environment": args.environment,
        "base_url": args.base_url,
        "model": args.model,
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform()},
        "dataset": {
            "path": str(args.dataset),
            "sha256": dataset_digest,
            "cases": len(cases),
        },
        "scoring": scoring,
        "request_failures": failures,
        "acceptance": {
            "minimum_accuracy": args.min_accuracy,
            "passed": scoring["accuracy"] >= args.min_accuracy and not failures,
        },
        "limitations": [
            "Scoring uses normalized exact match against accepted answers.",
            "This smoke suite does not measure general reasoning quality, groundedness, or hallucination rate.",
            "Use a reviewed, larger dataset and independent human/judge evaluation before release certification.",
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Report written to {args.output}")
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
