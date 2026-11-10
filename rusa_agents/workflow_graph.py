"""Validated workflow DAG primitives for bounded, tenant-safe task scheduling."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


class WorkflowGraphError(ValueError):
    """A workflow graph violates identity, dependency, cycle, or fan-out rules."""


@dataclass(frozen=True)
class WorkflowTaskSpec:
    task_id: str
    domain: str
    agent_id: str
    agent_version: str
    capability: str
    deadline_at: datetime
    dependencies: tuple[str, ...] = ()
    priority: int = 5
    max_attempts: int = 3
    budget: Mapping[str, Any] = field(default_factory=dict)
    input: Mapping[str, Any] = field(default_factory=dict)
    input_refs: tuple[Mapping[str, Any], ...] = ()
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "domain", "agent_id", "agent_version", "capability"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowGraphError(f"{name} must be a non-empty string")
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise WorkflowGraphError("task deadline_at must be timezone-aware")
        if not 0 <= self.priority <= 9:
            raise WorkflowGraphError("task priority must be between 0 and 9")
        if not 1 <= self.max_attempts <= 100:
            raise WorkflowGraphError("task max_attempts must be between 1 and 100")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise WorkflowGraphError("task dependencies must be unique")
        if self.task_id in self.dependencies:
            raise WorkflowGraphError("task cannot depend on itself")
        if not isinstance(self.budget, Mapping) or not isinstance(self.input, Mapping):
            raise WorkflowGraphError("task budget and input must be objects")


@dataclass(frozen=True)
class WorkflowRunSpec:
    tenant_id: str
    workflow_id: str
    run_id: str
    domain: str
    objective: str
    deadline_at: datetime
    created_by: str
    budget: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_id", "workflow_id", "run_id", "domain", "objective", "created_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowGraphError(f"{name} must be a non-empty string")
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise WorkflowGraphError("workflow deadline_at must be timezone-aware")
        if not isinstance(self.budget, Mapping):
            raise WorkflowGraphError("workflow budget must be an object")


@dataclass(frozen=True)
class WorkflowGraph:
    run: WorkflowRunSpec
    tasks: tuple[WorkflowTaskSpec, ...]
    max_tasks: int = 1000
    max_fan_out: int = 128

    def topological_order(self) -> tuple[str, ...]:
        if not self.tasks:
            raise WorkflowGraphError("workflow must contain at least one task")
        if len(self.tasks) > self.max_tasks:
            raise WorkflowGraphError("workflow exceeds the configured task limit")
        if self.max_tasks < 1 or self.max_fan_out < 1:
            raise WorkflowGraphError("workflow graph limits must be positive")

        by_id: dict[str, WorkflowTaskSpec] = {}
        for task in self.tasks:
            if task.task_id in by_id:
                raise WorkflowGraphError(f"duplicate task_id: {task.task_id}")
            by_id[task.task_id] = task

        dependents: dict[str, list[str]] = defaultdict(list)
        indegree = {task_id: 0 for task_id in by_id}
        for task in self.tasks:
            for dependency_id in task.dependencies:
                if dependency_id not in by_id:
                    raise WorkflowGraphError(
                        f"task {task.task_id} references missing dependency {dependency_id}"
                    )
                dependency = by_id[dependency_id]
                if dependency.task_id == task.task_id:
                    raise WorkflowGraphError("task cannot depend on itself")
                dependents[dependency_id].append(task.task_id)
                indegree[task.task_id] += 1

        oversized = {
            task_id: len(children)
            for task_id, children in dependents.items()
            if len(children) > self.max_fan_out
        }
        if oversized:
            task_id, count = next(iter(oversized.items()))
            raise WorkflowGraphError(
                f"task {task_id} exceeds maximum fan-out {self.max_fan_out} ({count})"
            )

        ready = deque(
            task_id for task_id in by_id if indegree[task_id] == 0
        )
        ordered: list[str] = []
        while ready:
            current = ready.popleft()
            ordered.append(current)
            for child in dependents[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)

        if len(ordered) != len(by_id):
            cyclic = sorted(task_id for task_id, degree in indegree.items() if degree > 0)
            raise WorkflowGraphError("workflow dependency graph contains a cycle: " + ", ".join(cyclic))
        return tuple(ordered)

    def validate(self) -> WorkflowGraph:
        self.topological_order()
        for task in self.tasks:
            if task.deadline_at > self.run.deadline_at:
                raise WorkflowGraphError(
                    f"task {task.task_id} deadline exceeds workflow deadline"
                )
        return self


def dependency_outcome(
    dependencies: Sequence[str],
    states: Mapping[str, str],
) -> str:
    """Return ready, waiting, or blocked using durable task status values."""
    if not dependencies:
        return "ready"
    terminal_failures = {"failed", "blocked", "cancelled", "skipped", "expired"}
    if any(states.get(task_id) in terminal_failures for task_id in dependencies):
        return "blocked"
    if all(states.get(task_id) == "succeeded" for task_id in dependencies):
        return "ready"
    return "waiting"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
