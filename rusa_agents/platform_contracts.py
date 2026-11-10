"""Versioned, tenant-aware contracts for the RUSA AI platform control plane.

These models are internal platform contracts. They do not replace the existing
A2A wire protocol in schemas/a2a/task-envelope.schema.json.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_SEMVER_PATTERN = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$"
_AGENT_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
_CAPABILITY_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
_TRACEPARENT_PATTERN = r"^00-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}$"
_SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "bearer_token",
        "authorization",
        "password",
        "client_secret",
        "private_key",
        "credential",
        "credentials",
        "secret",
    }
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _validate_traceparent(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.split("-")
    if len(parts) != 4 or parts[1] == "0" * 32 or parts[2] == "0" * 16:
        raise ValueError("traceparent must contain non-zero trace and parent span IDs")
    return value


def _reject_secret_fields(value: Any, path: str = "payload") -> None:
    """Reject credential-shaped keys before data enters durable contracts."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                raise ValueError(f"credential field is not allowed at {path}.{normalized}")
            _reject_secret_fields(child, f"{path}.{normalized}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


class ContractModel(BaseModel):
    """Common strictness for all platform contract models."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class AgentDomain(str, Enum):
    SHARED = "shared"
    SOFTWARE_ENGINEERING = "software_engineering"
    INTELLIGENCE = "intelligence"
    BUSINESS = "business"
    ECOSYSTEM = "ecosystem"


class AgentStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


class AgentHealthStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    MAINTENANCE = "maintenance"


class RuntimeKind(str, Enum):
    PYTHON_CALLABLE = "python_callable"
    HTTP = "http"
    CONTAINER = "container"


class TaskOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    AWAITING_APPROVAL = "awaiting_approval"


class ArtifactReference(ContractModel):
    """Reference to input/output content; digest is recommended for immutable data."""

    uri: str = Field(min_length=1, max_length=2048)
    media_type: str | None = Field(default=None, max_length=255)
    sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    label: str | None = Field(default=None, max_length=256)


class ImmutableArtifactReference(ContractModel):
    """Tenant-authorized immutable artifact reference bound to a SHA-256 digest."""

    uri: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    media_type: str | None = Field(default=None, max_length=255)
    label: str | None = Field(default=None, max_length=256)


class EvidenceReference(ContractModel):
    """Immutable evidence reference bound to a content digest and evaluated subject."""

    uri: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    kind: str = Field(min_length=1, max_length=128)
    producer_id: str = Field(min_length=1, max_length=128)
    producer_version: str | None = Field(default=None, max_length=64)
    subject_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    subject_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40,64}$")
    test_profile: str | None = Field(default=None, max_length=128)
    occurred_at: datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def evidence_timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None


class AgentHealthSnapshot(ContractModel):
    """Runtime health observation; not part of an agent's immutable executable version."""

    status: AgentHealthStatus = AgentHealthStatus.UNKNOWN
    checked_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=1000)

    @field_validator("checked_at")
    @classmethod
    def health_timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None


class RuntimeBinding(ContractModel):
    kind: RuntimeKind
    target: str = Field(min_length=1, max_length=512)
    protocol: str | None = Field(default=None, max_length=64)


class ModelPolicy(ContractModel):
    allowed_providers: list[str] = Field(default_factory=list, max_length=64)
    allowed_models: list[str] = Field(default_factory=list, max_length=128)
    allow_fallback: bool = False
    max_context_tokens: int | None = Field(default=None, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def unique_model_allowlists(self) -> ModelPolicy:
        if len(self.allowed_providers) != len(set(self.allowed_providers)):
            raise ValueError("allowed_providers must be unique")
        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("allowed_models must be unique")
        return self


class TenantScope(ContractModel):
    mode: Literal["tenant", "system", "allowlist"] = "tenant"
    tenant_ids: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_tenant_scope(self) -> TenantScope:
        if self.mode == "allowlist" and not self.tenant_ids:
            raise ValueError("allowlist tenant scope requires at least one tenant_id")
        if self.mode != "allowlist" and self.tenant_ids:
            raise ValueError("tenant_ids are only valid when scope mode is allowlist")
        if len(self.tenant_ids) != len(set(self.tenant_ids)):
            raise ValueError("tenant_ids must be unique")
        return self


class ResourceScope(ContractModel):
    include_patterns: list[str] = Field(default_factory=list, max_length=256)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=256)


class DeadlinePolicy(ContractModel):
    max_execution_seconds: int = Field(default=3600, ge=1, le=86_400)
    max_attempts: int = Field(default=3, ge=1, le=10)


class BudgetPolicy(ContractModel):
    max_tool_calls: int = Field(default=100, ge=1, le=10_000)
    max_tokens: int | None = Field(default=None, ge=1, le=100_000_000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1_000_000)
    max_parallel_tasks: int = Field(default=1, ge=1, le=100)


class AgentManifestV1(ContractModel):
    """Immutable, versioned description of an agent capability and its grants."""

    schema_version: Literal["1.0"] = "1.0"
    agent_id: str = Field(pattern=_AGENT_ID_PATTERN)
    domain: AgentDomain
    version: str = Field(pattern=_SEMVER_PATTERN)
    status: AgentStatus = AgentStatus.DRAFT
    capabilities: list[str] = Field(min_length=1, max_length=128)
    input_schema_ref: str = Field(min_length=1, max_length=2048)
    output_schema_ref: str = Field(min_length=1, max_length=2048)
    runtime_binding: RuntimeBinding
    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)
    tool_allowlist: list[str] = Field(default_factory=list, max_length=256)
    tenant_scope: TenantScope = Field(default_factory=TenantScope)
    resource_scope: ResourceScope = Field(default_factory=ResourceScope)
    deadline_policy: DeadlinePolicy = Field(default_factory=DeadlinePolicy)
    budget_policy: BudgetPolicy = Field(default_factory=BudgetPolicy)
    policy_profile: str = Field(min_length=1, max_length=128)
    approval_policy: list[str] = Field(default_factory=list, max_length=128)
    evaluation_profile: str = Field(min_length=1, max_length=128)
    health: AgentHealthSnapshot
    owner: str = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def validate_manifest(self) -> AgentManifestV1:
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must be unique")
        if len(self.tool_allowlist) != len(set(self.tool_allowlist)):
            raise ValueError("tool_allowlist must be unique")
        if len(self.approval_policy) != len(set(self.approval_policy)):
            raise ValueError("approval_policy must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        return self


class ExecutionBudget(ContractModel):
    """Per-task hard limits; defaults provide a bounded legacy migration envelope."""

    max_execution_seconds: int = Field(default=3600, ge=1, le=86_400)
    max_tool_calls: int = Field(default=100, ge=1, le=10_000)
    max_tokens: int | None = Field(default=None, ge=1, le=100_000_000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1_000_000)


class TaskEnvelopeV2(ContractModel):
    """Canonical internal task envelope for platform workflow execution."""

    schema_version: Literal["2.0"] = "2.0"
    tenant_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    correlation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    causation_id: str | None = Field(default=None, min_length=1, max_length=128)
    traceparent: str = Field(pattern=_TRACEPARENT_PATTERN)
    domain: AgentDomain
    agent_id: str = Field(pattern=_AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=_SEMVER_PATTERN)
    capability: str = Field(pattern=_CAPABILITY_PATTERN)
    attempt: int = Field(default=1, ge=1, le=100)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    priority: int = Field(default=5, ge=0, le=9)
    deadline_at: datetime
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    input: dict[str, Any] = Field(default_factory=dict)
    input_refs: list[ImmutableArtifactReference] = Field(default_factory=list, max_length=256)
    policy_context: dict[str, str] = Field(default_factory=dict)
    approval_required: bool = False
    approval_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("deadline_at", "created_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("traceparent")
    @classmethod
    def trace_context_has_nonzero_ids(cls, value: str | None) -> str | None:
        return _validate_traceparent(value)

    @field_validator("input", "policy_context", mode="before")
    @classmethod
    def reject_embedded_credentials(cls, value: Any) -> Any:
        _reject_secret_fields(value)
        return value

    @model_validator(mode="after")
    def validate_deadline(self) -> TaskEnvelopeV2:
        if self.deadline_at <= self.created_at:
            raise ValueError("deadline_at must be later than created_at")
        return self


class AgentResultV2(ContractModel):
    """Typed result. Legacy worker evidence is retained but is not a QA verdict."""

    schema_version: Literal["2.0"] = "2.0"
    result_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(pattern=_AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=_SEMVER_PATTERN)
    capability: str = Field(pattern=_CAPABILITY_PATTERN)
    attempt: int = Field(default=1, ge=1, le=100)
    status: TaskOutcome
    summary: str = Field(min_length=1, max_length=8000)
    output: dict[str, Any] = Field(default_factory=dict)
    output_refs: list[ImmutableArtifactReference] = Field(default_factory=list, max_length=256)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=256)
    legacy_evidence: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list, max_length=128)
    error: str | None = Field(default=None, max_length=4000)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("output", "legacy_evidence", mode="before")
    @classmethod
    def reject_embedded_credentials(cls, value: Any) -> Any:
        _reject_secret_fields(value)
        return value

    @model_validator(mode="after")
    def validate_result(self) -> AgentResultV2:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if len(self.blockers) != len(set(self.blockers)):
            raise ValueError("blockers must be unique")
        return self


class EventContractModel(BaseModel):
    """Event consumers ignore unknown optional fields within the same major version."""

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class TaskAcceptedPayloadV2(EventContractModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)


class TaskProgressPayloadV2(EventContractModel):
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    percent: int = Field(ge=0, le=100)
    stage: str = Field(min_length=1, max_length=128)


class ToolInvocationPayloadV2(EventContractModel):
    tool: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=128)
    input_ref: str = Field(min_length=1, max_length=2048)
    operation_id: str = Field(min_length=1, max_length=128)
    lease_epoch: int = Field(ge=1)


class TaskCompletedPayloadV2(EventContractModel):
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    result_ref: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^(?:sha256:)?[a-f0-9]{64}$")


class TaskFailureCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    POLICY = "POLICY"
    AMBIGUOUS = "AMBIGUOUS"


class SideEffectStatus(str, Enum):
    NONE = "NONE"
    APPLIED = "APPLIED"
    UNKNOWN = "UNKNOWN"


class TaskFailedPayloadV2(EventContractModel):
    lease_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    code: str = Field(min_length=1, max_length=128)
    category: TaskFailureCategory
    side_effect: SideEffectStatus
    diagnostic_ref: str | None = Field(default=None, max_length=2048)


class PolicyDecisionPayloadV2(EventContractModel):
    decision_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=128)
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    decision: Literal["allow", "deny", "require_approval"]
    action: str = Field(min_length=1, max_length=256)
    resource_ref: str = Field(min_length=1, max_length=2048)
    reason_code: str = Field(min_length=1, max_length=128)
    input_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluated_at: datetime = Field(default_factory=utc_now)

    @field_validator("evaluated_at")
    @classmethod
    def policy_timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class PolicyDecisionV1(ContractModel):
    """Versioned deterministic authorization decision; no model-supplied outcome."""

    schema_version: Literal["1.0"] = "1.0"
    decision_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=256)
    resource_ref: str = Field(min_length=1, max_length=2048)
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    outcome: Literal["allow", "deny", "require_approval"]
    reason_code: str = Field(min_length=1, max_length=128)
    input_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluated_at: datetime = Field(default_factory=utc_now)
    workflow_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    approval_id: str | None = Field(default=None, max_length=128)

    @field_validator("evaluated_at")
    @classmethod
    def decision_timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class PolicyViolationPayloadV2(EventContractModel):
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    decision_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=256)
    resource_ref: str = Field(min_length=1, max_length=2048)
    reason_code: str = Field(min_length=1, max_length=128)


class TaskCancellationRequestedPayloadV2(EventContractModel):
    cancel_epoch: int = Field(ge=1)
    reason_code: str = Field(min_length=1, max_length=128)


class TaskCancelledPayloadV2(EventContractModel):
    cancel_epoch: int = Field(ge=1)
    state_version: int = Field(ge=1)
    side_effect: SideEffectStatus


class ApprovalRequestPayloadV2(EventContractModel):
    approval_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    requested_by: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=256)
    resource_ref: str = Field(min_length=1, max_length=2048)
    artifact_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    reason: str = Field(min_length=1, max_length=2000)
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def approval_expiry_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None


class ApprovalDecisionPayloadV2(EventContractModel):
    approval_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approved", "rejected", "expired", "cancelled"]
    decided_by: str = Field(min_length=1, max_length=128)
    decided_at: datetime = Field(default_factory=utc_now)
    reason: str | None = Field(default=None, max_length=2000)
    policy_id: str | None = Field(default=None, max_length=128)
    policy_version: str | None = Field(default=None, max_length=64)

    @field_validator("decided_at")
    @classmethod
    def approval_decision_timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class ValidationVerdictPayloadV2(EventContractModel):
    verdict: Literal["passed", "failed", "inconclusive", "hold"]
    subject_digest: str = Field(pattern=_SHA256_PATTERN)
    producer_id: str = Field(min_length=1, max_length=128)
    test_profile: str = Field(min_length=1, max_length=128)
    evidence_refs: list[EvidenceReference] = Field(min_length=1, max_length=256)
    evaluated_at: datetime = Field(default_factory=utc_now)
    commit_sha: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{40,64}$",
    )
    summary: str = Field(min_length=1, max_length=4000)

    @field_validator("evaluated_at")
    @classmethod
    def verdict_timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def evidence_matches_subject(self) -> ValidationVerdictPayloadV2:
        for item in self.evidence_refs:
            if item.subject_digest != self.subject_digest:
                raise ValueError("validation evidence subject digest does not match verdict subject")
            if not item.producer_version or not item.test_profile or item.occurred_at is None:
                raise ValueError(
                    "validation evidence requires producer version, test profile, and timestamp"
                )
            if item.test_profile != self.test_profile:
                raise ValueError("validation evidence test profile does not match verdict")
            if self.commit_sha is not None and item.subject_commit != self.commit_sha:
                raise ValueError("validation evidence commit does not match verdict commit")
        return self


class AuditRecordedPayloadV2(EventContractModel):
    actor: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=256)
    decision: Literal["allow", "deny"]
    audit_ref: str = Field(min_length=1, max_length=2048)


EVENT_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "task.accepted": TaskAcceptedPayloadV2,
    "task.progress": TaskProgressPayloadV2,
    "tool.invocation": ToolInvocationPayloadV2,
    "task.completed": TaskCompletedPayloadV2,
    "task.failed": TaskFailedPayloadV2,
    "policy.decision": PolicyDecisionPayloadV2,
    "policy.violation": PolicyViolationPayloadV2,
    "task.cancel.requested": TaskCancellationRequestedPayloadV2,
    "task.cancelled": TaskCancelledPayloadV2,
    "approval.requested": ApprovalRequestPayloadV2,
    "approval.decided": ApprovalDecisionPayloadV2,
    "validation.verdict": ValidationVerdictPayloadV2,
    "audit.recorded": AuditRecordedPayloadV2,
}


class WorkflowEventV2(EventContractModel):
    """Versioned workflow event; known event payloads receive strict typed validation."""

    schema_version: Literal["2.0"] = "2.0"
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    attempt: int | None = Field(default=None, ge=1, le=100)
    message_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: str | None = Field(default=None, min_length=1, max_length=128)
    traceparent: str = Field(pattern=_TRACEPARENT_PATTERN)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    producer_id: str = Field(min_length=1, max_length=128)
    producer_version: str = Field(min_length=1, max_length=64)
    sequence: int | None = Field(default=None, ge=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("traceparent")
    @classmethod
    def trace_context_has_nonzero_ids(cls, value: str | None) -> str | None:
        return _validate_traceparent(value)

    @field_validator("payload", mode="before")
    @classmethod
    def reject_embedded_credentials(cls, value: Any) -> Any:
        _reject_secret_fields(value)
        return value

    @model_validator(mode="after")
    def validate_known_payload(self) -> WorkflowEventV2:
        task_scoped_events = {
            "task.accepted",
            "task.progress",
            "tool.invocation",
            "task.completed",
            "task.failed",
            "task.cancel.requested",
            "task.cancelled",
        }
        if self.event_type in task_scoped_events and (
            self.task_id is None or self.attempt is None
        ):
            raise ValueError("task lifecycle events require task_id and attempt")
        payload_model = EVENT_PAYLOAD_MODELS.get(self.event_type)
        if payload_model is not None:
            payload_model.model_validate(self.payload)
        return self


class AICodeTaskStateV1(str, Enum):
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


class AICodeQueueStatusV1(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRecordSnapshotV2(ContractModel):
    """Compatibility snapshot preserving the existing AICode TaskRecord state and results."""

    schema_version: Literal["2.0"] = "2.0"
    task: TaskEnvelopeV2
    state: AICodeTaskStateV1
    results: list[AgentResultV2] = Field(default_factory=list, max_length=256)
    errors: list[str] = Field(default_factory=list, max_length=256)
    updated_at: datetime = Field(default_factory=utc_now)
    queue_status: AICodeQueueStatusV1 | None = None

    @field_validator("updated_at")
    @classmethod
    def snapshot_timestamp_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)


def validate_manifest_update(
    previous: AgentManifestV1,
    candidate: AgentManifestV1,
) -> None:
    """Enforce immutable published manifest content while permitting lifecycle/health updates."""
    if candidate.updated_at < previous.updated_at:
        raise ValueError("manifest updated_at cannot move backwards")

    if previous.status != AgentStatus.DRAFT:
        if previous.agent_id != candidate.agent_id:
            raise ValueError("published agent_id is immutable")
        if previous.version != candidate.version:
            raise ValueError("published agent version is immutable; publish a new semantic version")
        ignored = {"status", "updated_at", "health"}
        before = previous.model_dump(exclude=ignored, mode="json")
        after = candidate.model_dump(exclude=ignored, mode="json")
        if before != after:
            raise ValueError("published agent manifest content is immutable")

    allowed_statuses = {
        AgentStatus.DRAFT: {AgentStatus.VALIDATED, AgentStatus.REVOKED},
        AgentStatus.VALIDATED: {
            AgentStatus.ACTIVE, AgentStatus.SUSPENDED, AgentStatus.DEPRECATED, AgentStatus.REVOKED,
        },
        AgentStatus.ACTIVE: {AgentStatus.SUSPENDED, AgentStatus.DEPRECATED, AgentStatus.REVOKED},
        AgentStatus.SUSPENDED: {AgentStatus.ACTIVE, AgentStatus.DEPRECATED, AgentStatus.REVOKED},
        AgentStatus.DEPRECATED: {AgentStatus.REVOKED},
        AgentStatus.REVOKED: set(),
    }
    if candidate.status != previous.status and candidate.status not in allowed_statuses[previous.status]:
        raise ValueError(
            f"invalid agent status transition: {previous.status.value} -> {candidate.status.value}"
        )




PLATFORM_SCHEMA_MODELS: dict[str, tuple[type[BaseModel], str]] = {
    "agent-manifest.v1.schema.json": (
        AgentManifestV1,
        "https://schemas.rusa.ai/platform/agent-manifest.v1.schema.json",
    ),
    "task-envelope.v2.schema.json": (
        TaskEnvelopeV2,
        "https://schemas.rusa.ai/platform/task-envelope.v2.schema.json",
    ),
    "agent-result.v2.schema.json": (
        AgentResultV2,
        "https://schemas.rusa.ai/platform/agent-result.v2.schema.json",
    ),
    "workflow-event.v2.schema.json": (
        WorkflowEventV2,
        "https://schemas.rusa.ai/platform/workflow-event.v2.schema.json",
    ),
    "policy-decision.v1.schema.json": (
        PolicyDecisionV1,
        "https://schemas.rusa.ai/platform/policy-decision.v1.schema.json",
    ),
    "task-record.v2.schema.json": (
        TaskRecordSnapshotV2,
        "https://schemas.rusa.ai/platform/task-record.v2.schema.json",
    ),
}


def platform_contract_schema(model: type[BaseModel], schema_id: str) -> dict[str, Any]:
    """Return the checked-in JSON Schema representation for a Pydantic contract."""
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = _SCHEMA_DRAFT
    schema["$id"] = schema_id

    if model is WorkflowEventV2:
        definitions = schema.setdefault("$defs", {})
        conditions: list[dict[str, Any]] = []
        for event_type, payload_model in EVENT_PAYLOAD_MODELS.items():
            payload_schema = payload_model.model_json_schema(
                mode="validation",
                ref_template="#/$defs/{model}",
            )
            nested_definitions = payload_schema.pop("$defs", {})
            for name, definition in nested_definitions.items():
                existing = definitions.get(name)
                if existing is not None and existing != definition:
                    raise ValueError(f"conflicting event schema definition: {name}")
                definitions[name] = definition
            definitions[payload_model.__name__] = payload_schema
            condition = {
                "if": {"properties": {"event_type": {"const": event_type}}},
                "then": {
                    "properties": {
                        "payload": {"$ref": f"#/$defs/{payload_model.__name__}"}
                    }
                },
            }
            if event_type in {
                "task.accepted",
                "task.progress",
                "tool.invocation",
                "task.completed",
                "task.failed",
                "task.cancel.requested",
                "task.cancelled",
            }:
                condition["then"]["required"] = ["task_id", "attempt"]
            conditions.append(condition)
        schema["allOf"] = conditions

    return schema


def normalize_contract_schema(value: Any) -> Any:
    """Remove non-validation annotations that can vary across Pydantic minor releases."""
    annotations = {"title", "description", "examples", "deprecated"}
    if isinstance(value, dict):
        return {
            key: normalize_contract_schema(item)
            for key, item in value.items()
            if key not in annotations
        }
    if isinstance(value, list):
        return [normalize_contract_schema(item) for item in value]
    return value
