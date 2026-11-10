"""Versioned, tenant-aware contracts for the RUSA AI platform control plane.

These models are internal platform contracts. They do not replace the existing
A2A wire protocol in schemas/a2a/task-envelope.schema.json.
"""
from __future__ import annotations

import re
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
_TRACEPARENT_PATTERN = r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
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


class EvidenceReference(ContractModel):
    """Immutable evidence reference bound to a content digest."""

    uri: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    kind: str = Field(min_length=1, max_length=128)
    producer_id: str = Field(min_length=1, max_length=128)
    subject_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)


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
    traceparent: str | None = Field(default=None, pattern=_TRACEPARENT_PATTERN)
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
    input_refs: list[ArtifactReference] = Field(default_factory=list, max_length=256)
    policy_context: dict[str, str] = Field(default_factory=dict)
    approval_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("deadline_at", "created_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value)

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
    output_refs: list[ArtifactReference] = Field(default_factory=list, max_length=256)
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


class WorkflowEventV2(ContractModel):
    """Versioned event envelope for workflow lifecycle and audit projections."""

    schema_version: Literal["2.0"] = "2.0"
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    message_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: str | None = Field(default=None, min_length=1, max_length=128)
    traceparent: str | None = Field(default=None, pattern=_TRACEPARENT_PATTERN)
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

    @field_validator("payload", mode="before")
    @classmethod
    def reject_embedded_credentials(cls, value: Any) -> Any:
        _reject_secret_fields(value)
        return value


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
}


def platform_contract_schema(model: type[BaseModel], schema_id: str) -> dict[str, Any]:
    """Return the checked-in JSON Schema representation for a Pydantic contract."""
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = _SCHEMA_DRAFT
    schema["$id"] = schema_id
    return schema
