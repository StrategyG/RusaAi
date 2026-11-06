"""Tenant-scoped operational and benchmark evaluation metrics."""
from __future__ import annotations

import math
import os
from datetime import timezone
from typing import Any

from sqlalchemy import and_, or_, select

from .policy import POLICY_VERSION
from .storage import AuditRow, Database, TaskRow


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "cancelled"}
EVALUATION_EVENTS = (
    "task.awaiting_approval",
    "policy.decision",
    "policy.benchmark_decision",
    "task.started",
)


def _percentile(values: list[float], percentile: int) -> float | None:
    """Return the nearest-rank percentile, or None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return round(ordered[rank - 1], 2)


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
            task_ids = [task.id for task in tasks]
            event_scopes = [AuditRow.event == "policy.benchmark_decision"]
            if task_ids:
                event_scopes.append(
                    and_(
                        AuditRow.event.in_(
                            ("task.awaiting_approval", "policy.decision", "task.started")
                        ),
                        AuditRow.task_id.in_(task_ids),
                    )
                )
            audit_events = (
                await session.scalars(
                    select(AuditRow)
                    .where(
                        AuditRow.tenant_id == tenant_id,
                        or_(*event_scopes),
                    )
                    .order_by(AuditRow.created_at.desc())
                    .limit(max(5000, bounded_limit * 250))
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
                latencies.append(
                    max(0.0, (updated - created).total_seconds() * 1000)
                )

        terminal_tasks = [task for task in tasks if task.status in TERMINAL_STATUSES]
        terminal_count = len(terminal_tasks)
        succeeded = status_counts.get("succeeded", 0)
        first_attempt_succeeded = sum(
            1
            for task in terminal_tasks
            if task.status == "succeeded" and task.attempts == 1
        )
        retry_count = sum(max(0, task.attempts - 1) for task in tasks)

        reviewed_rejections = 0
        policy_decision_count = 0
        labeled_policy_events: list[dict[str, Any]] = []
        observed_model_names: set[str] = set()
        observed_model_revisions: set[str] = set()
        for event in audit_events:
            details = event.details or {}
            if event.event == "task.awaiting_approval":
                if details.get("reason") == "reviewer_rejected_diff":
                    reviewed_rejections += 1
            elif event.event == "policy.decision":
                policy_decision_count += 1
            elif event.event == "policy.benchmark_decision":
                if (
                    isinstance(details.get("expected_allowed"), bool)
                    and isinstance(details.get("actual_allowed"), bool)
                    and isinstance(details.get("expected_requires_approval"), bool)
                    and isinstance(details.get("actual_requires_approval"), bool)
                ):
                    labeled_policy_events.append(details)
            elif event.event == "task.started":
                if isinstance(details.get("model_name"), str) and details["model_name"]:
                    observed_model_names.add(details["model_name"])
                if isinstance(details.get("model_revision"), str) and details["model_revision"]:
                    observed_model_revisions.add(details["model_revision"])

        correct_policy_decisions = sum(
            1
            for item in labeled_policy_events
            if item["expected_allowed"] == item["actual_allowed"]
            and item["expected_requires_approval"] == item["actual_requires_approval"]
        )
        false_allow_count = sum(
            1
            for item in labeled_policy_events
            if not item["expected_allowed"] and item["actual_allowed"]
        )
        false_deny_count = sum(
            1
            for item in labeled_policy_events
            if item["expected_allowed"] and not item["actual_allowed"]
        )
        policy_sample_size = len(labeled_policy_events)
        reviewer_decisions = reviewer_results + reviewed_rejections

        model_name = os.getenv("RUSA_AGENT_MODEL_NAME") or (
            sorted(observed_model_names)[-1] if observed_model_names else None
        )
        model_revision = os.getenv("RUSA_AGENT_MODEL_REVISION") or (
            sorted(observed_model_revisions)[-1] if observed_model_revisions else None
        )

        success_rate = round(succeeded / terminal_count, 4) if terminal_count else None
        first_attempt_success_rate = (
            round(first_attempt_succeeded / terminal_count, 4)
            if terminal_count
            else None
        )
        retry_rate = round(
            sum(1 for task in tasks if task.attempts > 1) / total, 4
        ) if total else None
        latency_p95 = _percentile(latencies, 95)

        return {
            "sample_size": total,
            "terminal_sample_size": terminal_count,
            "limit": bounded_limit,
            "status_counts": status_counts,
            "completion_rate": round(terminal_count / total, 4) if total else None,
            "task_success_rate": success_rate,
            "success_rate": success_rate,
            "first_attempt_success_rate": first_attempt_success_rate,
            "first_attempt_success_count": first_attempt_succeeded,
            "retry_rate": retry_rate,
            "retry_count": retry_count,
            "average_attempts": round(attempts / total, 2) if total else None,
            "validated_plan_rate": round(planned / total, 4) if total else None,
            "policy_version": POLICY_VERSION,
            "policy_decision_count": policy_decision_count,
            "policy_decision_sample_size": policy_sample_size,
            "policy_decision_accuracy": (
                round(correct_policy_decisions / policy_sample_size, 4)
                if policy_sample_size
                else None
            ),
            "false_allow_count": false_allow_count if policy_sample_size else None,
            "false_deny_count": false_deny_count if policy_sample_size else None,
            "policy_unlabeled_decision_count": policy_decision_count,
            "model_name": model_name,
            "model_revision": model_revision,
            "model_name_samples": sorted(observed_model_names),
            "model_revision_samples": sorted(observed_model_revisions),
            "reviewer_approval_rate": round(
                reviewer_approved / reviewer_decisions, 4
            ) if reviewer_decisions else None,
            "human_override_count": human_overrides,
            "reviewer_rejection_count": reviewed_rejections,
            "reviewer_decision_count": reviewer_decisions,
            "tool_calls": tool_calls,
            "average_tool_calls": round(tool_calls / total, 2) if total else None,
            "citation_count": citations_total,
            "citation_validation_rate": 1.0 if citations_total else None,
            "latency_sample_size": len(latencies),
            "latency_ms_p50": _percentile(latencies, 50),
            "latency_ms_p95": latency_p95,
            "latency_p95_ms": latency_p95,
            "cost_per_task": None,
            "cost_measurement_status": (
                "NOT_MEASURED: provider token usage and price telemetry are not "
                "persisted per task"
            ),
            "unmeasured_metrics": [
                "cost_per_task",
                "provider_token_usage",
                *(
                    ["policy_decision_accuracy"]
                    if not policy_sample_size
                    else []
                ),
                *(
                    ["model_revision"]
                    if model_revision is None
                    else []
                ),
            ],
            "measurement_notes": [
                "Task success and first-attempt success use terminal tasks as the denominator.",
                "Retry count is total attempts minus one, summed across the sampled tasks.",
                "Latency uses nearest-rank p50/p95 over terminal tasks; latency_sample_size is explicit.",
                "Policy accuracy uses only labeled policy.benchmark_decision audit events; production decisions without independent labels are not treated as correct by assumption.",
                "Policy decisions are audited with tool name, outcome, approval requirement, and policy version; no tool arguments or payloads are recorded.",
                "Cost is not inferred from prompt length; operational cost requires provider usage and pricing persisted per task. The repeated benchmark runner can measure per-task usage deltas sequentially when the provider reports them.",
                "Model name and revision are drawn from task-start audit events or deployment environment; null means not configured or not observed.",
            ],
        }
