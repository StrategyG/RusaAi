"""Versioned contracts for RUSA AI's Agentic DevOps lifecycle.

This module defines the Phase 1 message and identity contracts only. It does not
introduce another queue, broker, or execution runtime. Durable delivery continues
to use the existing PostgreSQL task queue until a later architecture phase.
"""
from __future__ import annotations

import datetime
import enum
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TRACEPARENT_PATTERN = (
    r"^00-(?!0{32}-)[a-f0-9]{32}-(?!0{16}-)[a-f0-9]{16}-[a-f0-9]{2}$"
)
SHA256_PATTERN = r"^[a-f0-9]{64}$"
AGENT_ID_PATTERN = r"^[a-z][a-z0-9_-]{1,63}$"
IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


class ContractModel(BaseModel):
    """Base model: reject undeclared fields and normalize surrounding whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgentStage(str, enum.Enum):
    PLAN = "plan"
    CODE = "code"
    BUILD = "build"
    TEST = "test"
    RELEASE = "release"
    DEPLOY = "deploy"
    OPERATE = "operate"
    MONITOR = "monitor"


class AgentTaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_APPROVAL = "needs_approval"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Priority(str, enum.Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class AgentKind(str, enum.Enum):
    ORCHESTRATOR = "orchestrator"
    SPECIALIST = "specialist"
    SECURITY_GUARD = "security_guard"
    SUPPORT = "support"


class EventType(str, enum.Enum):
    TASK_REQUEST = "task.request"
    TASK_ACCEPTED = "task.accepted"
    TASK_PROGRESS = "task.progress"
    TOOL_INVOCATION = "tool.invocation"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    POLICY_VIOLATION = "policy.violation"
    TASK_CANCEL_REQUESTED = "task.cancel.requested"
    TASK_CANCELLED = "task.cancelled"
    AUDIT_RECORDED = "audit.recorded"


class FailureCategory(str, enum.Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    POLICY = "POLICY"
    AMBIGUOUS = "AMBIGUOUS"


class SideEffect(str, enum.Enum):
    NONE = "NONE"
    APPLIED = "APPLIED"
    UNKNOWN = "UNKNOWN"


def _require_timezone(value: datetime.datetime, field_name: str) -> datetime.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


class ArtifactReference(ContractModel):
    """Immutable artifact pointer with a content digest; authorization is checked by the consumer."""

    uri: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str | None = Field(default=None, max_length=128)


class AgentConstraints(ContractModel):
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    network_access: bool = False
    write_scope: list[str] = Field(default_factory=list, max_length=100)
    approval_required: bool = False

    @field_validator("allowed_tools", "write_scope")
    @classmethod
    def validate_unique_nonempty_entries(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 256 for item in values):
            raise ValueError("constraint entries must be non-empty and bounded")
        if len(values) != len(set(values)):
            raise ValueError("constraint entries must be unique")
        return values

    @field_validator("write_scope")
    @classmethod
    def reject_unsafe_write_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            path = pathlib.PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("write_scope must contain relative paths without traversal")
        return values


class AgentIdentity(ContractModel):
    """Stable agent identity and declared capability boundary."""

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=120)
    kind: AgentKind
    stage: AgentStage | None = None
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    policy_scopes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_kind_stage(self) -> AgentIdentity:
        if self.kind == AgentKind.SPECIALIST and self.stage is None:
            raise ValueError("specialist agents must declare one lifecycle stage")
        if self.kind != AgentKind.SPECIALIST and self.stage is not None:
            raise ValueError("only specialist agents may declare a lifecycle stage")
        for field_name in ("capabilities", "allowed_tools", "policy_scopes"):
            values = getattr(self, field_name)
            if any(not item.strip() or len(item) > 256 for item in values):
                raise ValueError(f"{field_name} entries must be non-empty and bounded")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} entries must be unique")
        return self


class AgentTask(ContractModel):
    """A durable delegation request for one specialist agent."""

    schema_version: typing.Literal[1]
    task_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    parent_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_agent: str = Field(pattern=AGENT_ID_PATTERN)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    stage: AgentStage
    objective: str = Field(min_length=1, max_length=4000)
    input_refs: list[ArtifactReference] = Field(default_factory=list, max_length=100)
    context: dict[str, typing.Any] = Field(default_factory=dict)
    constraints: AgentConstraints = Field(default_factory=AgentConstraints)
    priority: Priority = Priority.P2
    acceptance_criteria: list[str] = Field(min_length=1, max_length=100)
    deadline_at: datetime.datetime
    attempt: int = Field(ge=1, le=100)
    idempotency_key: str = Field(pattern=IDEMPOTENCY_PATTERN)
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: str | None = Field(default=None, min_length=1, max_length=128)
    traceparent: str = Field(pattern=TRACEPARENT_PATTERN)
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 1000 for item in values):
            raise ValueError("acceptance criteria must be non-empty and bounded")
        return values

    @field_validator("deadline_at", "created_at")
    @classmethod
    def validate_timestamps(cls, value: datetime.datetime, info):
        return _require_timezone(value, info.field_name)


class AgentResult(ContractModel):
    """A specialist's report, bound to task/workflow and immutable evidence."""

    schema_version: typing.Literal[1]
    task_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    stage: AgentStage
    status: AgentTaskStatus
    attempt: int = Field(ge=1, le=100)
    summary: str = Field(min_length=1, max_length=8000)
    artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=100)
    evidence: list[ArtifactReference] = Field(default_factory=list, max_length=100)
    risks: list[str] = Field(default_factory=list, max_length=100)
    unresolved_issues: list[str] = Field(default_factory=list, max_length=100)
    next_actions: list[str] = Field(default_factory=list, max_length=100)
    started_at: datetime.datetime | None = None
    completed_at: datetime.datetime | None = None
    model_id: str | None = Field(default=None, max_length=200)
    policy_version: str | None = Field(default=None, max_length=128)

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_optional_timestamps(cls, value: datetime.datetime | None, info):
        return _require_timezone(value, info.field_name) if value is not None else None

    @field_validator("risks", "unresolved_issues", "next_actions")
    @classmethod
    def validate_report_entries(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 2000 for item in values):
            raise ValueError("report entries must be non-empty and bounded")
        return values

    @model_validator(mode="after")
    def validate_completion_evidence(self) -> AgentResult:
        terminal = {
            AgentTaskStatus.PASSED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.BLOCKED,
            AgentTaskStatus.NEEDS_APPROVAL,
            AgentTaskStatus.CANCELLED,
            AgentTaskStatus.SKIPPED,
        }
        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal agent results must include completed_at")
        if self.status == AgentTaskStatus.PASSED and not self.evidence:
            raise ValueError("passed agent results must include evidence")
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at cannot precede started_at")
        return self


class TaskRequestPayload(ContractModel):
    capability: str = Field(min_length=1, max_length=128)
    input_ref: str = Field(min_length=1, max_length=2048)
    deadline_at: datetime.datetime
    idempotency_key: str = Field(pattern=IDEMPOTENCY_PATTERN)
    priority: Priority

    @field_validator("deadline_at")
    @classmethod
    def validate_deadline(cls, value: datetime.datetime):
        return _require_timezone(value, "deadline_at")


class TaskAcceptedPayload(ContractModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)


class TaskProgressPayload(ContractModel):
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    percent: int = Field(ge=0, le=100)
    stage: str = Field(min_length=1, max_length=128)


class ToolInvocationPayload(ContractModel):
    tool: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    input_ref: str = Field(min_length=1, max_length=2048)
    operation_id: str = Field(min_length=1, max_length=128)
    lease_epoch: int = Field(ge=1)


class TaskCompletedPayload(ContractModel):
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    result_ref: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=SHA256_PATTERN)


class TaskFailedPayload(ContractModel):
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    code: str = Field(min_length=1, max_length=128)
    category: FailureCategory
    side_effect: SideEffect
    diagnostic_ref: str | None = Field(default=None, max_length=2048)


class PolicyViolationPayload(ContractModel):
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    resource_ref: str = Field(min_length=1, max_length=2048)
    reason_code: str = Field(min_length=1, max_length=128)


class TaskCancelRequestedPayload(ContractModel):
    cancel_epoch: int = Field(ge=1)
    reason_code: str = Field(min_length=1, max_length=128)


class TaskCancelledPayload(ContractModel):
    cancel_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    side_effect: SideEffect


class AuditRecordedPayload(ContractModel):
    action: str = Field(min_length=1, max_length=128)
    outcome: str = Field(min_length=1, max_length=128)
    resource_ref: str = Field(min_length=1, max_length=2048)
    policy_version: str | None = Field(default=None, max_length=128)
    state_version: int | None = Field(default=None, ge=1)


EVENT_PAYLOAD_MODELS: dict[EventType, type[ContractModel]] = {
    EventType.TASK_REQUEST: TaskRequestPayload,
    EventType.TASK_ACCEPTED: TaskAcceptedPayload,
    EventType.TASK_PROGRESS: TaskProgressPayload,
    EventType.TOOL_INVOCATION: ToolInvocationPayload,
    EventType.TASK_COMPLETED: TaskCompletedPayload,
    EventType.TASK_FAILED: TaskFailedPayload,
    EventType.POLICY_VIOLATION: PolicyViolationPayload,
    EventType.TASK_CANCEL_REQUESTED: TaskCancelRequestedPayload,
    EventType.TASK_CANCELLED: TaskCancelledPayload,
    EventType.AUDIT_RECORDED: AuditRecordedPayload,
}
TASK_SCOPED_EVENTS = {
    EventType.TASK_REQUEST,
    EventType.TASK_ACCEPTED,
    EventType.TASK_PROGRESS,
    EventType.TOOL_INVOCATION,
    EventType.TASK_COMPLETED,
    EventType.TASK_FAILED,
    EventType.TASK_CANCEL_REQUESTED,
    EventType.TASK_CANCELLED,
}


class WorkflowEvent(ContractModel):
    """Versioned JSON event envelope compatible with the communication ADR."""

    schema_version: typing.Literal[1]
    type: EventType
    message_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    attempt: int | None = Field(default=None, ge=1, le=100)
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: str | None = Field(default=None, min_length=1, max_length=128)
    traceparent: str = Field(pattern=TRACEPARENT_PATTERN)
    producer: str = Field(min_length=1, max_length=128)
    occurred_at: datetime.datetime
    payload: dict[str, typing.Any]

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime.datetime):
        return _require_timezone(value, "occurred_at")

    @model_validator(mode="after")
    def validate_event_contract(self) -> WorkflowEvent:
        if self.type in TASK_SCOPED_EVENTS and (self.task_id is None or self.attempt is None):
            raise ValueError("task lifecycle events require task_id and attempt")
        payload_model = EVENT_PAYLOAD_MODELS[self.type]
        payload_model.model_validate(self.payload)
        return self


LEGACY_AICODE_STATUS_MAP = {
    "received": AgentTaskStatus.QUEUED,
    "queued": AgentTaskStatus.QUEUED,
    "observing": AgentTaskStatus.RUNNING,
    "planning": AgentTaskStatus.RUNNING,
    "executing": AgentTaskStatus.RUNNING,
    "verifying": AgentTaskStatus.RUNNING,
    "running": AgentTaskStatus.RUNNING,
    "awaiting_approval": AgentTaskStatus.NEEDS_APPROVAL,
    "succeeded": AgentTaskStatus.PASSED,
    "passed": AgentTaskStatus.PASSED,
    "failed": AgentTaskStatus.FAILED,
    "blocked": AgentTaskStatus.BLOCKED,
    "cancelled": AgentTaskStatus.CANCELLED,
}


def map_aicode_task_state(state: Any) -> AgentTaskStatus:
    """Map existing AICode/queue states to the Agentic DevOps public status vocabulary."""
    value = getattr(state, "value", state)
    if not isinstance(value, str) or value not in LEGACY_AICODE_STATUS_MAP:
        raise ValueError(f"unsupported legacy AICode task state: {value!r}")
    return LEGACY_AICODE_STATUS_MAP[value]
