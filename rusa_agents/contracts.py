"""Versioned contracts for hierarchical RUSA-AI agent execution."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkerRole(StrEnum):
    SUPERVISOR = "supervisor"
    ANALYST = "analyst"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    TESTER = "tester"
    SECURITY = "security"


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=8, max_length=8000)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(
        default="main",
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._/-]+$",
    )
    work_branch: str = Field(min_length=3, max_length=255)
    context: str = Field(default="", max_length=20000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=30)
    allowed_tools: list[str] = Field(
        default_factory=lambda: [
            "github.list_files",
            "github.read_file",
            "github.create_branch",
            "github.write_file",
            "github.compare_branches",
            "agent.review_diff",
            "github.get_ci_status",
            "github.create_pull_request",
            "rag.search",
        ],
        max_length=20,
    )
    max_attempts: int = Field(default=3, ge=1, le=10)
    require_ci: bool = True
    require_pull_request: bool = True

    @field_validator("work_branch")
    @classmethod
    def validate_work_branch(cls, value: str) -> str:
        if not re.fullmatch(r"(feature|fix|chore|docs)/[A-Za-z0-9._/-]+", value):
            raise ValueError("work_branch must use a safe feature/, fix/, chore/, or docs/ ref")
        if any(segment in {"", ".", ".."} for segment in value.split("/")):
            raise ValueError("work_branch contains an unsafe path segment")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_criteria(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("acceptance criteria cannot be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_branch_separation(self) -> "TaskCreate":
        if self.work_branch == self.base_branch:
            raise ValueError("work_branch must never equal base_branch")
        return self


class ToolStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str = Field(min_length=1, max_length=1000)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[str] = Field(default_factory=list, max_length=30)
    risk_level: Literal["low", "medium", "high", "critical"]


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[ToolStep] = Field(min_length=1, max_length=24)
    expected_outcome: str = Field(default="", max_length=4000)


class WorkerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["1.0"] = "1.0"
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    correlation_id: str
    from_agent: WorkerRole
    to_agent: WorkerRole
    message_type: Literal[
        "task.delegate", "task.accepted", "task.progress", "task.result",
        "task.blocked", "task.cancel",
    ]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)


ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.QUEUED, TaskStatus.AWAITING_APPROVAL, TaskStatus.SUCCEEDED,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED,
    },
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: {TaskStatus.QUEUED},
    TaskStatus.BLOCKED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.CANCELLED: set(),
}


def validate_transition(current: TaskStatus | str, target: TaskStatus | str) -> None:
    current_state = TaskStatus(current)
    target_state = TaskStatus(target)
    if target_state not in ALLOWED_TRANSITIONS[current_state]:
        raise ValueError(f"invalid task transition: {current_state.value} -> {target_state.value}")
