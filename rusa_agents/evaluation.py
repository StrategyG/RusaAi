"""Tenant-scoped operational evaluation metrics for agent task runs."""
from __future__ import annotations

from datetime import timezone
from typing import Any

from sqlalchemy import select

from .storage import AuditRow, Database, TaskRow


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "cancelled"}


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)


class AgentEvaluator:
    def __init__(self, database: Database):
        self.database = database

    async def metrics(self, tenant_id: str, limit: int = 100) -> dict[str, Any]:
        bounded_limit = max(1, min(int(limit), 1000))
        async with self.database.session() as session:
            tasks = (
                await session.scalars(
                    select(TaskRow)
                    .where(TaskRow.tenant_id == tenant_id)
                    .order_by(TaskRow.created_at.desc())
                    .limit(bounded_limit)
                )
            ).all()
            audit_events = (
                await session.scalars(
                    select(AuditRow).where(
                        AuditRow.tenant_id == tenant_id,
                        AuditRow.event == "task.awaiting_approval",
                    ).order_by(AuditRow.created_at.desc()).limit(bounded_limit * 10)
                )
            ).all()

        total = len(tasks)
        status_counts: dict[str, int] = {}
        latencies: list[float] = []
        attempts = 0
        planned = 0
        tool_calls = 0
        reviewer_results = 0
        reviewer_approved = 0
        human_overrides = 0
        citations_total = 0

        for task in tasks:
            status_counts[task.status] = status_counts.get(task.status, 0) + 1
            attempts += task.attempts
            planned += int(task.plan is not None)
            results = task.tool_results or []
            tool_calls += len(results)
            for item in results:
                if item.get("tool") != "agent.review_diff":
                    continue
                reviewer_results += 1
                review = item.get("result") or {}
                if review.get("approved"):
                    reviewer_approved += 1
                if review.get("human_override"):
                    human_overrides += 1
            final_answer = (task.result or {}).get("final_answer") or {}
            citations_total += len(final_answer.get("citations") or [])
            if task.status in TERMINAL_STATUSES:
                created = task.created_at
                updated = task.updated_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                latencies.append(max(0.0, (updated - created).total_seconds() * 1000))

        completed = sum(status_counts.get(name, 0) for name in TERMINAL_STATUSES)
        succeeded = status_counts.get("succeeded", 0)
        reviewed_rejections = sum(
            1
            for event in audit_events
            if (event.details or {}).get("reason") == "reviewer_rejected_diff"
        )
        reviewer_decisions = reviewer_results + reviewed_rejections
        return {
            "sample_size": total,
            "limit": bounded_limit,
            "status_counts": status_counts,
            "completion_rate": round(completed / total, 4) if total else None,
            "success_rate": round(succeeded / completed, 4) if completed else None,
            "failure_rate": round(status_counts.get("failed", 0) / completed, 4) if completed else None,
            "retry_rate": round(
                sum(1 for task in tasks if task.attempts > 1) / total,
                4,
            ) if total else None,
            "average_attempts": round(attempts / total, 2) if total else None,
            "validated_plan_rate": round(planned / total, 4) if total else None,
            "reviewer_approval_rate": round(
                reviewer_approved / reviewer_decisions,
                4,
            ) if reviewer_decisions else None,
            "human_override_count": human_overrides,
            "reviewer_rejection_count": reviewed_rejections,
            "reviewer_decision_count": reviewer_decisions,
            "tool_calls": tool_calls,
            "average_tool_calls": round(tool_calls / total, 2) if total else None,
            "citation_count": citations_total,
            "citation_validation_rate": 1.0 if citations_total else None,
            "latency_ms_p50": _percentile(latencies, 50),
            "latency_ms_p95": _percentile(latencies, 95),
            "measurement_notes": [
                "Rates are calculated over the most recent tenant-scoped task sample.",
                "Plans counted as validated passed deterministic plan validation before persistence.",
                "Citation validation rate reflects the synthesis citation allowlist check, not human source-quality review.",
                "Model token usage, monetary cost, policy violation rate, and benchmark accuracy require additional instrumentation and are not inferred.",
            ],
        }
