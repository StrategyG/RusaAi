"""Validated contracts and task state transitions for the AICode runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4


_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_BRANCH_RE = re.compile(r"^(feature|fix|chore|docs)/[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
_SHA_RE = re.compile(r"^[a-fA-F0-9]{40}$")


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


class EvidenceKind(str, Enum):
    TEST_RUN = "test_run"
    SECURITY_SCAN = "security_scan"


@dataclass(frozen=True)
class RepositoryTarget:
    full_name: str
    base_branch: str = "main"
    work_branch: str = "feature/aicode-task"

    def __post_init__(self) -> None:
        parts = self.full_name.split("/")
        if len(parts) != 2 or not all(_OWNER_RE.fullmatch(part) for part in parts):
            raise ValueError("repository must use a valid owner/name identifier")
        if not self.base_branch or not self.work_branch:
            raise ValueError("branch names cannot be empty")
        if self.work_branch == self.base_branch:
            raise ValueError("work branch must not equal the protected base branch")
        if not _BRANCH_RE.fullmatch(self.work_branch):
            raise ValueError("work branch must use an approved feature/fix/chore/docs prefix")
        if (
            ".." in self.work_branch
            or "//" in self.work_branch
            or self.work_branch.endswith("/")
            or self.work_branch.endswith(".")
            or "@{" in self.work_branch
        ):
            raise ValueError("work branch contains an unsafe Git ref sequence")


@dataclass
class CodingTask:
    objective: str
    repository: RepositoryTarget
    acceptance_criteria: list[str]
    tenant_id: str
    context: str = ""
    task_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not self.objective.strip() or len(self.objective) > 8000:
            raise ValueError("objective must contain 1-8000 non-whitespace characters")
        if not self.tenant_id.strip() or len(self.tenant_id) > 128:
            raise ValueError("tenant_id must contain 1-128 non-whitespace characters")
        if not self.acceptance_criteria:
            raise ValueError("at least one acceptance criterion is required")
        if any(not item.strip() or len(item) > 2000 for item in self.acceptance_criteria):
            raise ValueError("acceptance criteria must be non-empty and at most 2000 characters")
        if len(self.context) > 20000:
            raise ValueError("context exceeds the 20000 character limit")
        try:
            UUID(self.task_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("task_id must be a UUID") from exc


@dataclass(frozen=True)
class Evidence:
    kind: EvidenceKind
    reference: str
    commit_sha: str

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("evidence reference cannot be empty")
        if not _SHA_RE.fullmatch(self.commit_sha):
            raise ValueError("evidence commit_sha must be a full 40-character Git SHA")


@dataclass
class WorkerResult:
    role: WorkerRole
    summary: str
    success: bool
    artifacts: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


_ALLOWED_TRANSITIONS = {
    TaskState.RECEIVED: {TaskState.OBSERVING, TaskState.CANCELLED},
    TaskState.OBSERVING: {TaskState.PLANNING, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.PLANNING: {
        TaskState.EXECUTING,
        TaskState.AWAITING_APPROVAL,
        TaskState.BLOCKED,
        TaskState.FAILED,
    },
    TaskState.EXECUTING: {
        TaskState.VERIFYING,
        TaskState.AWAITING_APPROVAL,
        TaskState.BLOCKED,
        TaskState.FAILED,
    },
    TaskState.VERIFYING: {
        TaskState.SUCCEEDED,
        TaskState.AWAITING_APPROVAL,
        TaskState.BLOCKED,
        TaskState.FAILED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.EXECUTING,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
    },
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED: set(),
    TaskState.BLOCKED: set(),
    TaskState.CANCELLED: set(),
}


@dataclass
class TaskRecord:
    task: CodingTask
    state: TaskState = TaskState.RECEIVED
    results: list[WorkerResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def transition(self, new_state: TaskState) -> None:
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(
                f"invalid task transition: {self.state.value} -> {new_state.value}"
            )
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()
