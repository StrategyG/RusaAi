"""Task-scoped capability authorization immediately before Tool Gateway execution."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .agent_tools import ROLE_TOOL_ALLOWLIST
from .contracts import WorkerRole
from .platform_contracts import AgentHealthStatus, AgentManifestV1, AgentStatus, PolicyDecisionV1
from .registry import _tenant_allowed


class CapabilityDenied(PermissionError):
    """A tool invocation is outside the active manifest, task grant, or policy decision."""


@dataclass(frozen=True)
class TaskScopedGrant:
    tenant_id: str
    task_id: str
    agent_id: str
    agent_version: str
    capability: str
    allowed_tools: frozenset[str]
    expires_at: datetime
    grant_id: str
    resource_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all((
            self.tenant_id, self.task_id, self.agent_id, self.agent_version,
            self.capability, self.grant_id,
        )):
            raise ValueError("task-scoped grant identity fields are required")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("task-scoped grant expiry must be timezone-aware")
        if not self.allowed_tools:
            raise ValueError("task-scoped grant must contain at least one tool")


@dataclass(frozen=True)
class ToolAuthorizationContext:
    tenant_id: str
    registry_tenant_id: str
    task_id: str
    agent_id: str
    agent_version: str
    capability: str
    tool_name: str
    resource_ref: str
    worker_role: WorkerRole
    task_deadline_at: datetime
    policy_version: str
    grant: TaskScopedGrant
    policy_decision: PolicyDecisionV1


def authorize_tool_invocation(
    manifest: AgentManifestV1,
    context: ToolAuthorizationContext,
    *,
    now: datetime | None = None,
) -> None:
    """Fail closed unless every tenant, version, tool, resource, deadline, and policy check passes."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("authorization clock must be timezone-aware")
    instant = instant.astimezone(timezone.utc)

    grant = context.grant
    decision = context.policy_decision
    if manifest.status != AgentStatus.ACTIVE:
        raise CapabilityDenied("agent version is not active")
    if manifest.health.status != AgentHealthStatus.HEALTHY:
        raise CapabilityDenied("agent runtime health is not healthy")
    health_checked_at = manifest.health.checked_at
    if health_checked_at is None:
        raise CapabilityDenied("agent runtime health snapshot has no timestamp")
    health_age = instant - health_checked_at.astimezone(timezone.utc)
    if health_age < timedelta(0) or health_age > timedelta(minutes=5):
        raise CapabilityDenied("agent runtime health snapshot is stale or from the future")
    if (
        manifest.agent_id != context.agent_id
        or manifest.version != context.agent_version
        or context.capability not in manifest.capabilities
    ):
        raise CapabilityDenied("manifest identity or capability does not match the task")
    if not _tenant_allowed(manifest, context.tenant_id, context.registry_tenant_id):
        raise CapabilityDenied("manifest tenant scope does not authorize this tenant")

    if (
        context.tenant_id != grant.tenant_id
        or context.task_id != grant.task_id
        or context.agent_id != grant.agent_id
        or context.agent_version != grant.agent_version
        or context.capability != grant.capability
    ):
        raise CapabilityDenied("task-scoped grant does not match the invocation identity")
    if instant >= grant.expires_at.astimezone(timezone.utc):
        raise CapabilityDenied("task-scoped grant has expired")
    if context.task_deadline_at.tzinfo is None or context.task_deadline_at.utcoffset() is None:
        raise CapabilityDenied("task deadline must be timezone-aware")
    if instant >= context.task_deadline_at.astimezone(timezone.utc):
        raise CapabilityDenied("task deadline has expired")

    role_tools = ROLE_TOOL_ALLOWLIST.get(context.worker_role, frozenset())
    if (
        context.tool_name not in manifest.tool_allowlist
        or context.tool_name not in grant.allowed_tools
        or context.tool_name not in role_tools
    ):
        raise CapabilityDenied("tool is not allowed by manifest, task grant, and legacy role policy")

    resource = context.resource_ref
    if not resource:
        raise CapabilityDenied("resource reference is required")
    scope = manifest.resource_scope
    if not scope.include_patterns:
        raise CapabilityDenied("manifest resource scope has no explicit include patterns")
    if not any(fnmatch.fnmatchcase(resource, pattern) for pattern in scope.include_patterns):
        raise CapabilityDenied("resource is outside the manifest include scope")
    if any(fnmatch.fnmatchcase(resource, pattern) for pattern in scope.exclude_patterns):
        raise CapabilityDenied("resource is excluded by the manifest resource scope")
    if not grant.resource_patterns:
        raise CapabilityDenied("task-scoped grant has no explicit resource patterns")
    if not any(
        fnmatch.fnmatchcase(resource, pattern) for pattern in grant.resource_patterns
    ):
        raise CapabilityDenied("resource is outside the task-scoped grant")

    if (
        decision.tenant_id != context.tenant_id
        or decision.actor != context.agent_id
        or decision.task_id != context.task_id
        or decision.policy_id != manifest.policy_profile
        or decision.policy_version != context.policy_version
        or decision.evaluated_at > instant
        or instant - decision.evaluated_at.astimezone(timezone.utc) > timedelta(minutes=5)
        or decision.action != f"tool.invoke:{context.tool_name}"
        or decision.resource_ref != resource
        or decision.outcome != "allow"
    ):
        raise CapabilityDenied("deterministic policy decision does not authorize this exact tool resource")
