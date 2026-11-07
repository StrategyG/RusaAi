"""Typed task contracts and a guarded lifecycle state machine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class TaskState(str, Enum):
    RECEIVED = "received"
    OBSERVING = "observing"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkerRole(str, Enum):
    ANALYST = "analyst"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    DEBUGGER = "debugger"
    TESTER = "tester"
    SECURITY = "security"


@dataclass(frozen=True)
class RepositoryTarget:
    full_name: str
    base_branch: str = "main"
    work_branch: str = "feature/aicode-task"

    def __post_init__(self) -> None:
        if self.full_name.count("/") != 1:
            raise ValueError("repository must use owner/name format")
        if not self.base_branch or not self.work_branch:
            raise ValueError("branch names cannot be empty")


@dataclass
class CodingTask:
    objective: str
    repository: RepositoryTarget
    acceptance_criteria: list[str]
    context: str = ""
    task_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("objective cannot be empty")
        if not self.acceptance_criteria or any(not item.strip() for item in self.acceptance_criteria):
            raise ValueError("non-empty acceptance criteria are required")


@dataclass
class WorkerResult:
    role: WorkerRole
    summary: str
    success: bool
    artifacts: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)


@dataclass
class TaskRecord:
    task: CodingTask
    state: TaskState = TaskState.RECEIVED
    results: list[WorkerResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    qa_verdict: dict[str, Any] | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    TRANSITIONS = {
        TaskState.RECEIVED: {TaskState.OBSERVING, TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED},
        TaskState.OBSERVING: {TaskState.PLANNING, TaskState.BLOCKED, TaskState.FAILED},
        TaskState.PLANNING: {TaskState.EXECUTING, TaskState.BLOCKED, TaskState.FAILED},
        TaskState.EXECUTING: {TaskState.VERIFYING, TaskState.BLOCKED, TaskState.FAILED},
        TaskState.VERIFYING: {
            TaskState.EXECUTING, TaskState.SUCCEEDED, TaskState.AWAITING_APPROVAL,
            TaskState.BLOCKED, TaskState.FAILED,
        },
        TaskState.AWAITING_APPROVAL: {
            TaskState.SUCCEEDED, TaskState.BLOCKED, TaskState.CANCELLED,
        },
        TaskState.SUCCEEDED: set(),
        TaskState.FAILED: set(),
        TaskState.BLOCKED: set(),
        TaskState.CANCELLED: set(),
    }

    def transition(self, new_state: TaskState) -> None:
        if new_state not in self.TRANSITIONS[self.state]:
            raise ValueError(f"invalid task transition: {self.state.value} -> {new_state.value}")
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()
