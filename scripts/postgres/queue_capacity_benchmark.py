#!/usr/bin/env python3
"""Measure PostgreSQL task-queue admission and claim throughput without running agents."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg

from rusa_agents.contracts import CodingTask, RepositoryTarget
from rusa_agents.queue import PostgresTaskQueue


def build_task(tenant_id: str, index: int) -> CodingTask:
    task_id = uuid4().hex
    idempotency_key = f"capacity-benchmark-{index}-{uuid4().hex}"
    work_branch = CodingTask.expected_work_branch_for(
        tenant_id, idempotency_key, task_id
    )
    return CodingTask(
        tenant_id=tenant_id,
        objective=f"Synthetic queue capacity probe {index}",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=work_branch,
        ),
        acceptance_criteria=("Queue claim is measured; no repository write is performed.",),
        idempotency_key=idempotency_key,
        task_id=task_id,
    )


def percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999) - 1))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=500)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--lease-seconds", type=int, default=60)
    args = parser.parse_args()

    if not 1 <= args.tasks <= 100_000:
        parser.error("--tasks must be between 1 and 100000")
    if not 1 <= args.workers <= 256:
        parser.error("--workers must be between 1 and 256")
    if not 30 <= args.lease_seconds <= 1800:
        parser.error("--lease-seconds must be between 30 and 1800")

    api_dsn = os.getenv("RUSA_AGENT_API_DATABASE_URL")
    worker_dsn = os.getenv("RUSA_AGENT_DATABASE_URL")
    if not api_dsn or not worker_dsn:
        parser.error(
            "RUSA_AGENT_API_DATABASE_URL and RUSA_AGENT_DATABASE_URL are required"
        )

    api_queue = PostgresTaskQueue(api_dsn)
    worker_queue = PostgresTaskQueue(worker_dsn)
    if not api_queue.check_ready() or not worker_queue.check_ready():
        parser.error("Agent queue schema or database role permissions are not ready")
    if args.tasks > api_queue.max_outstanding_per_tenant:
        parser.error(
            "--tasks cannot exceed RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT"
        )

    tenant_id = f"capacity-benchmark-{uuid4().hex}"
    tasks = [build_task(tenant_id, index) for index in range(args.tasks)]
    started = time.perf_counter()
    enqueue_started = time.perf_counter()
    try:
        for task in tasks:
            api_queue.enqueue(task)
        enqueue_seconds = max(time.perf_counter() - enqueue_started, 0.000001)

        claim_latencies: list[float] = []

        def claim_until_empty(index: int) -> int:
            claimed_count = 0
            while True:
                call_started = time.perf_counter()
                claimed = worker_queue.claim(
                    f"capacity-bench-{index}",
                    lease_seconds=args.lease_seconds,
                )
                claim_latencies.append(time.perf_counter() - call_started)
                if claimed is None:
                    return claimed_count
                claimed_count += 1

        claim_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            claimed_counts = list(
                executor.map(claim_until_empty, range(args.workers))
            )
        claim_seconds = max(time.perf_counter() - claim_started, 0.000001)
        claimed_total = sum(claimed_counts)
        if claimed_total != args.tasks:
            raise RuntimeError(
                f"expected {args.tasks} claims but observed {claimed_total}"
            )

        total_seconds = time.perf_counter() - started
        print(json.dumps({
            "tenant_id": tenant_id,
            "tasks": args.tasks,
            "worker_processes_simulated": args.workers,
            "enqueue_seconds": round(enqueue_seconds, 4),
            "enqueue_tasks_per_second": round(args.tasks / enqueue_seconds, 2),
            "claim_seconds": round(claim_seconds, 4),
            "claim_tasks_per_second": round(claimed_total / claim_seconds, 2),
            "p95_claim_latency_ms": round(percentile_95(claim_latencies) * 1000, 2),
            "total_seconds": round(total_seconds, 4),
            "scope": "database queue only; agent execution and model inference are excluded",
        }, indent=2))
        return 0
    finally:
        # The benchmark uses a unique tenant and removes only its own synthetic rows.
        with psycopg.connect(worker_dsn, connect_timeout=5) as connection:
            connection.execute(
                "DELETE FROM rusa_agent_tasks WHERE tenant_id=%s",
                (tenant_id,),
            )


if __name__ == "__main__":
    raise SystemExit(main())
