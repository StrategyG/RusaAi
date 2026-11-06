"""Validated task, worker, evidence, and lifecycle contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


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
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.full_name):
            raise ValueError("repository must use owner/name format")
        for label, branch in (("base", self.base_branch), ("work", self.work_branch)):
            if (
                not branch
                or branch.startswith("/")
                or branch.endswith("/")
                or "//" in branch
                or ".." in branch
                or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
            ):
                raise ValueError(f"invalid {label} branch name")
        if self.base_branch == self.work_branch:
            raise ValueError("work branch must not equal the protected base branch")
        if not re.match(r"^(feature|fix|chore|docs)/", self.work_branch):
            raise ValueError("work branch must use feature/, fix/, chore/, or docs/ prefix")


@dataclass
class CodingTask:
    objective: str
    repository: RepositoryTarget
    acceptance_criteria: list[str]
    context: str = ""
    tenant_id: str = "default"
    task_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.objective = self.objective.strip()
        if not self.objective or len(self.objective) > 8000:
            raise ValueError("objective must contain 1 to 8000 characters")
        if not self.acceptance_criteria or len(self.acceptance_criteria) > 50:
            raise ValueError("provide between 1 and 50 acceptance criteria")
        self.acceptance_criteria = [str(item).strip() for item in self.acceptance_criteria]
        if any(not item or len(item) > 1000 for item in self.acceptance_criteria):
            raise ValueError("each acceptance criterion must contain 1 to 1000 characters")
        if len(self.context) > 20000:
            raise ValueError("context must not exceed 20000 characters")
        if not self.tenant_id or len(self.tenant_id) > 128:
            raise ValueError("tenant_id must contain 1 to 128 characters")
        try:
            UUID(self.task_id)
        except ValueError as exc:
            raise ValueError("task_id must be a UUID") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "repository": {
                "full_name": self.repository.full_name,
                "base_branch": self.repository.base_branch,
                "work_branch": self.repository.work_branch,
            },
            "acceptance_criteria": self.acceptance_criteria,
            "context": self.context,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], tenant_id: str | None = None) -> CodingTask:
        repository = payload["repository"]
        return cls(
            objective=payload["objective"],
            repository=RepositoryTarget(
                full_name=repository["full_name"],
                base_branch=repository.get("base_branch", "main"),
                work_branch=repository["work_branch"],
            ),
            acceptance_criteria=list(payload["acceptance_criteria"]),
            context=payload.get("context", ""),
            tenant_id=tenant_id or payload.get("tenant_id", "default"),
            task_id=payload.get("task_id", str(uuid4())),
            created_at=payload.get("created_at", datetime.now(timezone.utc).isoformat()),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class ToolEvidence:
    kind: str
    name: str
    status: str
    source: str = ""
    sha: str = ""
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class WorkerResult:
    role: WorkerRole
    summary: str
    success: bool
    artifacts: list[str] = field(default_factory=list)
    evidence: list[ToolEvidence] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@dataclass
class TaskRecord:
    task: CodingTask
    state: TaskState = TaskState.RECEIVED
    results: list[WorkerResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    _TRANSITIONS = {
        TaskState.RECEIVED: {TaskState.OBSERVING, TaskState.CANCELLED},
        TaskState.OBSERVING: {TaskState.PLANNING, TaskState.BLOCKED, TaskState.FAILED},
        TaskState.PLANNING: {TaskState.EXECUTING, TaskState.BLOCKED, TaskState.FAILED},
        TaskState.EXECUTING: {
            TaskState.VERIFYING,
            TaskState.BLOCKED,
            TaskState.AWAITING_APPROVAL,
            TaskState.FAILED,
        },
        TaskState.VERIFYING: {
            TaskState.SUCCEEDED,
            TaskState.BLOCKED,
            TaskState.AWAITING_APPROVAL,
            TaskState.FAILED,
        },
        TaskState.AWAITING_APPROVAL: {TaskState.SUCCEEDED, TaskState.BLOCKED, TaskState.CANCELLED},
        TaskState.SUCCEEDED: set(),
        TaskState.FAILED: set(),
        TaskState.BLOCKED: set(),
        TaskState.CANCELLED: set(),
    }

    def transition(self, new_state: TaskState) -> None:
        if new_state not in self._TRANSITIONS[self.state]:
            raise ValueError(f"invalid task transition: {self.state.value} -> {new_state.value}")
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()
