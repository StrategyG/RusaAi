"""Validated task and worker contracts for the AICode runtime."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

class TaskState(StrEnum):
    RECEIVED="received"; OBSERVING="observing"; PLANNING="planning"
    EXECUTING="executing"; VERIFYING="verifying"; AWAITING_APPROVAL="awaiting_approval"
    SUCCEEDED="succeeded"; FAILED="failed"; BLOCKED="blocked"; CANCELLED="cancelled"

class WorkerRole(StrEnum):
    ANALYST="analyst"; ARCHITECT="architect"; DEVELOPER="developer"
    TESTER="tester"; SECURITY="security"

@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    full_name: str
    base_branch: str = "main"
    work_branch: str = "feature/aicode-task"
    def __post_init__(self):
        if self.full_name.count("/") != 1 or any(not p for p in self.full_name.split("/")):
            raise ValueError("repository must use owner/name format")
        if not self.base_branch.strip() or not self.work_branch.strip():
            raise ValueError("branch names cannot be empty")

@dataclass(slots=True)
class CodingTask:
    objective: str
    repository: RepositoryTarget
    acceptance_criteria: list[str]
    context: str = ""
    task_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    def __post_init__(self):
        self.objective = self.objective.strip()
        self.acceptance_criteria = [x.strip() for x in self.acceptance_criteria if x.strip()]
        if not self.objective or not self.acceptance_criteria:
            raise ValueError("objective and acceptance criteria are required")
        if len(self.objective) > 8000 or len(self.context) > 20000 or len(self.acceptance_criteria) > 50:
            raise ValueError("task input exceeds configured limits")

@dataclass(slots=True)
class WorkerResult:
    role: WorkerRole
    summary: str
    success: bool
    artifacts: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

@dataclass(slots=True)
class TaskRecord:
    task: CodingTask
    state: TaskState = TaskState.RECEIVED
    results: list[WorkerResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    _TRANSITIONS = {
        TaskState.RECEIVED:{TaskState.OBSERVING,TaskState.CANCELLED},
        TaskState.OBSERVING:{TaskState.PLANNING,TaskState.BLOCKED,TaskState.FAILED},
        TaskState.PLANNING:{TaskState.EXECUTING,TaskState.BLOCKED,TaskState.FAILED},
        TaskState.EXECUTING:{TaskState.VERIFYING,TaskState.BLOCKED,TaskState.FAILED},
        TaskState.VERIFYING:{TaskState.SUCCEEDED,TaskState.AWAITING_APPROVAL,TaskState.BLOCKED,TaskState.FAILED},
        TaskState.AWAITING_APPROVAL:{TaskState.SUCCEEDED,TaskState.BLOCKED,TaskState.CANCELLED},
        TaskState.SUCCEEDED:set(),TaskState.FAILED:set(),TaskState.BLOCKED:set(),TaskState.CANCELLED:set(),
    }
    def transition(self, new_state: TaskState):
        if new_state not in self._TRANSITIONS[self.state]:
            raise ValueError(f"invalid task transition: {self.state.value} -> {new_state.value}")
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()
