"""Typed task, worker, tool, and checkpoint contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4


class TaskState(str, Enum):
    RECEIVED = "received"
    OBSERVING = "observing"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    WAITING_EXTERNAL = "waiting_external"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerRole(str, Enum):
    ANALYST = "analyst"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    DEBUGGER = "debugger"
    TESTER = "tester"
    SECURITY = "security"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class RepositoryTarget:
    full_name: str
    base_branch: str = "main"
    work_branch: str = "rusa-agent/unassigned/unassigned"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.full_name):
            raise ValueError("repository must use owner/name format")
        for branch in (self.base_branch, self.work_branch):
            parts = branch.split("/")
            if (
                not branch or len(branch) > 128 or branch.startswith("/") or branch.endswith("/")
                or ".." in branch or "//" in branch or "@{" in branch
                or branch.endswith((".lock", "."))
                or any(part.startswith(".") or part.endswith(".") for part in parts)
                or any(char in branch for char in (" ", "~", "^", ":", "?", "*", "[", "\\"))
            ):
                raise ValueError("invalid git branch name")
        if self.work_branch == self.base_branch:
            raise ValueError("work_branch must differ from base_branch")
        if self.work_branch in {"main", "master", "production", "release"}:
            raise ValueError("work_branch cannot be a protected branch")
        if self.work_branch.startswith(("main/", "master/", "production/", "release/")):
            raise ValueError("work_branch cannot use a protected branch namespace")


@dataclass(frozen=True)
class CodingTask:
    tenant_id: str
    objective: str
    repository: RepositoryTarget
    acceptance_criteria: tuple[str, ...]
    context: str = ""
    idempotency_key: str = ""
    task_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Mapping[str, str] = field(default_factory=dict)

    @staticmethod
    def expected_work_branch_for(
        tenant_id: str, idempotency_key: str, task_id: str,
    ) -> str:
        identity = idempotency_key or task_id
        tenant_part = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:10]
        identity_part = hashlib.sha256(
            f"{tenant_id}:{identity}".encode("utf-8")
        ).hexdigest()[:24]
        return f"rusa-agent/{tenant_part}/{identity_part}"

    def expected_work_branch(self) -> str:
        return self.expected_work_branch_for(
            self.tenant_id, self.idempotency_key, self.task_id,
        )

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.tenant_id):
            raise ValueError("tenant_id is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.task_id):
            raise ValueError("task_id is invalid")
        if self.idempotency_key and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", self.idempotency_key
        ):
            raise ValueError("idempotency_key is invalid")
        if not self.objective.strip() or len(self.objective) > 4000:
            raise ValueError("objective must contain 1 to 4000 characters")
        if len(self.context) > 16000:
            raise ValueError("context exceeds 16000 characters")
        if not 1 <= len(self.acceptance_criteria) <= 20:
            raise ValueError("provide between 1 and 20 acceptance criteria")
        if any(not item.strip() or len(item) > 500 for item in self.acceptance_criteria):
            raise ValueError("each acceptance criterion must contain 1 to 500 characters")
        if len(set(self.acceptance_criteria)) != len(self.acceptance_criteria):
            raise ValueError("acceptance criteria must be unique")
        if len(self.metadata) > 32:
            raise ValueError("metadata contains too many entries")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.metadata.items()
        ):
            raise ValueError("task metadata keys and values must be strings")
        sensitive = ("secret", "password", "token", "credential", "api_key", "private_key")
        if any(any(word in key.lower() for word in sensitive) for key in self.metadata):
            raise ValueError("sensitive values must not be placed in task metadata")
        if self.repository.work_branch != self.expected_work_branch():
            raise ValueError("work_branch must be the task's tenant-scoped RUSA-AI branch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "objective": self.objective,
            "repository": {
                "full_name": self.repository.full_name,
                "base_branch": self.repository.base_branch,
                "work_branch": self.repository.work_branch,
            },
            "acceptance_criteria": list(self.acceptance_criteria),
            "context": self.context,
            "idempotency_key": self.idempotency_key or self.task_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CodingTask:
        return cls(
            tenant_id=str(value["tenant_id"]),
            objective=str(value["objective"]),
            repository=RepositoryTarget(**value["repository"]),
            acceptance_criteria=tuple(value["acceptance_criteria"]),
            context=str(value.get("context", "")),
            idempotency_key=str(value.get("idempotency_key", "")),
            task_id=str(value["task_id"]),
            created_at=str(value["created_at"]),
            metadata=dict(value.get("metadata", {})),
        )


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    name: str
    success: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerResult:
    role: WorkerRole
    summary: str
    tool_calls: tuple[ToolCall, ...] = ()
    blockers: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    tool_results: tuple[ToolResult, ...] = ()
    criteria_results: Mapping[str, bool] = field(default_factory=dict)


@dataclass
class TaskRecord:
    task: CodingTask
    state: TaskState = TaskState.RECEIVED
    worker_results: list[WorkerResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stage_index: int = 0
    repair_cycles: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    _TRANSITIONS = {
        TaskState.RECEIVED: {TaskState.OBSERVING, TaskState.CANCELLED, TaskState.FAILED},
        TaskState.OBSERVING: {
            TaskState.PLANNING, TaskState.BLOCKED, TaskState.FAILED, TaskState.AWAITING_APPROVAL,
        },
        TaskState.PLANNING: {
            TaskState.EXECUTING, TaskState.BLOCKED, TaskState.FAILED, TaskState.AWAITING_APPROVAL,
        },
        TaskState.EXECUTING: {
            TaskState.VERIFYING, TaskState.BLOCKED, TaskState.FAILED, TaskState.AWAITING_APPROVAL,
        },
        TaskState.VERIFYING: {
            TaskState.EXECUTING, TaskState.WAITING_EXTERNAL, TaskState.SUCCEEDED,
            TaskState.BLOCKED, TaskState.FAILED, TaskState.AWAITING_APPROVAL,
        },
        TaskState.WAITING_EXTERNAL: {
            TaskState.VERIFYING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED,
        },
        TaskState.AWAITING_APPROVAL: {
            TaskState.BLOCKED, TaskState.CANCELLED, TaskState.FAILED,
        },
        TaskState.SUCCEEDED: set(),
        TaskState.BLOCKED: set(),
        TaskState.FAILED: set(),
        TaskState.CANCELLED: set(),
    }

    def transition(self, new_state: TaskState) -> None:
        if new_state not in self._TRANSITIONS[self.state]:
            raise ValueError(f"invalid task transition: {self.state.value} -> {new_state.value}")
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "state": self.state.value,
            "stage_index": self.stage_index,
            "repair_cycles": self.repair_cycles,
            "worker_results": [
                {
                    "role": item.role.value,
                    "summary": item.summary,
                    "tool_calls": [
                        {"name": call.name, "arguments": dict(call.arguments)}
                        for call in item.tool_calls
                    ],
                    "blockers": list(item.blockers),
                    "risk": item.risk.value,
                    "tool_results": [
                        {
                            "name": result.name,
                            "success": result.success,
                            "data": dict(result.data),
                            "evidence": dict(result.evidence),
                        }
                        for result in item.tool_results
                    ],
                    "criteria_results": dict(item.criteria_results),
                }
                for item in self.worker_results
            ],
            "errors": list(self.errors),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskRecord:
        record = cls(
            task=CodingTask.from_dict(value["task"]),
            state=TaskState(value["state"]),
            errors=list(value.get("errors", [])),
            stage_index=int(value.get("stage_index", 0)),
            repair_cycles=int(value.get("repair_cycles", 0)),
            created_at=str(value.get("created_at", datetime.now(timezone.utc).isoformat())),
            updated_at=str(value.get("updated_at", datetime.now(timezone.utc).isoformat())),
        )
        for item in value.get("worker_results", []):
            calls = tuple(
                ToolCall(name=call["name"], arguments=call.get("arguments", {}))
                for call in item.get("tool_calls", [])
            )
            results = tuple(
                ToolResult(
                    name=result["name"], success=bool(result["success"]),
                    data=result.get("data", {}), evidence=result.get("evidence", {}),
                )
                for result in item.get("tool_results", [])
            )
            record.worker_results.append(
                WorkerResult(
                    role=WorkerRole(item["role"]),
                    summary=item["summary"],
                    tool_calls=calls,
                    blockers=tuple(item.get("blockers", [])),
                    risk=RiskLevel(item.get("risk", "low")),
                    tool_results=results,
                    criteria_results=item.get("criteria_results", {}),
                )
            )
        return record
