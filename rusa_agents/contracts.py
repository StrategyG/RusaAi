"""Typed contracts and state machine for hierarchical agent workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRole(str, Enum):
    SUPERVISOR = "supervisor"
    MANAGER = "manager"
    SPECIALIST = "specialist"
    WORKER = "worker"
    REVIEWER = "reviewer"


class TaskState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.BLOCKED,
    TaskState.CANCELLED,
}

_ALLOWED_TRANSITIONS = {
    TaskState.CREATED: {TaskState.QUEUED, TaskState.CANCELLED},
    TaskState.QUEUED: {TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.RUNNING: {
        TaskState.VERIFYING,
        TaskState.FAILED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
    },
    TaskState.VERIFYING: {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.BLOCKED,
    },
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED: set(),
    TaskState.BLOCKED: set(),
    TaskState.CANCELLED: set(),
}


@dataclass(frozen=True)
class TaskSpec:
    objective: str
    acceptance_criteria: tuple[str, ...]
    tenant_id: str
    project_id: str
    capability: str
    task_id: str = field(default_factory=lambda: str(uuid4()))
    parent_task_id: str | None = None
    dependencies: tuple[str, ...] = ()
    requested_by: str = "system"
    risk: str = "low"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("objective", "tenant_id", "project_id", "capability"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} cannot be empty")
        if not self.acceptance_criteria:
            raise ValueError("at least one acceptance criterion is required")
        if any(not item.strip() for item in self.acceptance_criteria):
            raise ValueError("acceptance criteria cannot contain empty values")
        if self.task_id in self.dependencies:
            raise ValueError("a task cannot depend on itself")


@dataclass(frozen=True)
class Evidence:
    kind: str
    reference: str
    description: str = ""


@dataclass(frozen=True)
class WorkerResult:
    task_id: str
    worker_id: str
    success: bool
    summary: str
    completed_criteria: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    security_violations: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskRecord:
    spec: TaskSpec
    state: TaskState = TaskState.CREATED
    attempts: int = 0
    result: WorkerResult | None = None
    error: str | None = None
    updated_at: str = field(default_factory=utc_now)

    def transition(self, new_state: TaskState) -> None:
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(
                f"invalid task transition: {self.state.value} -> {new_state.value}"
            )
        self.state = new_state
        self.updated_at = utc_now()
