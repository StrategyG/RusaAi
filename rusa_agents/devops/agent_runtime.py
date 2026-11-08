"""Fail-closed registry and capability boundary for DevOps specialist agents.

The registry describes what an identity may do. A separate policy grant
constrains each task to tenant-, resource-, approval-, and budget-specific
permissions. Task JSON is never itself an authorization grant.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from .contracts import validate_agent_result_for_task, validate_agent_task


class AgentAuthorizationError(PermissionError):
    """The task, workload identity, or policy grant is not authorized."""


class AgentCapabilityDeniedError(AgentAuthorizationError):
    """The task requests a tool or resource outside its registered grant."""


class AgentApprovalRequiredError(AgentAuthorizationError):
    """A required human approval is missing, stale, or outside its scope."""


class AgentBudgetExceededError(AgentAuthorizationError):
    """The task budget exceeds the independently issued policy grant."""


class AgentHandlerNotRegisteredError(LookupError):
    """No execution adapter is registered for the authenticated agent."""


class ToolInvocationDeniedError(AgentAuthorizationError):
    """A tool call attempted to exceed its immutable execution grant."""


class ToolInvoker(Protocol):
    async def invoke(self, tool_id: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    stage: str | None
    allowed_tools: frozenset[str]
    read_only: bool = False
    network_eligible: bool = False


@dataclass(frozen=True)
class ExecutionPolicyGrant:
    """Trusted, short-lived decision returned by the Policy Engine."""

    tenant_id: str
    principal: str
    policy_version: str
    allowed_tools: frozenset[str] = frozenset()
    write_scope: tuple[str, ...] = ()
    network_access: bool = False
    allowed_network_destinations: frozenset[str] = frozenset()
    approved_scopes: frozenset[str] = frozenset()
    approved_refs: frozenset[str] = frozenset()
    allowed_branches: frozenset[str] = frozenset()
    allowed_repositories: frozenset[str] = frozenset()
    allowed_artifact_refs: frozenset[str] = frozenset()
    allowed_runbooks: frozenset[str] = frozenset()
    allowed_environments: frozenset[str] = frozenset()
    max_cost_usd: float = 0.0
    max_tool_calls: int = 0
    max_runtime_seconds: int = 1

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.tenant_id, self.principal, self.policy_version)
        ):
            raise ValueError("policy grant tenant, principal, and version are required")
        if (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, (int, float))
            or not math.isfinite(self.max_cost_usd)
            or self.max_cost_usd < 0
        ):
            raise ValueError("policy max_cost_usd must be finite and non-negative")
        if (
            isinstance(self.max_tool_calls, bool)
            or not isinstance(self.max_tool_calls, int)
            or self.max_tool_calls < 0
        ):
            raise ValueError("policy max_tool_calls must be a non-negative integer")
        if (
            isinstance(self.max_runtime_seconds, bool)
            or not isinstance(self.max_runtime_seconds, int)
            or self.max_runtime_seconds < 1
        ):
            raise ValueError("policy max_runtime_seconds must be a positive integer")
        if not isinstance(self.network_access, bool):
            raise ValueError("policy network_access must be boolean")
        if not isinstance(self.allowed_tools, frozenset):
            raise TypeError("policy allowed_tools must be an immutable frozenset")
        if not isinstance(self.write_scope, tuple):
            raise TypeError("policy write_scope must be an immutable tuple")
        if not isinstance(self.allowed_network_destinations, frozenset):
            raise TypeError("policy network destinations must be an immutable frozenset")
        if not isinstance(self.approved_scopes, frozenset) or not isinstance(self.approved_refs, frozenset):
            raise TypeError("policy approvals must use immutable frozensets")
        if any(not isinstance(ref, str) or not ref for ref in self.approved_refs):
            raise ValueError("policy approval reference is invalid")
        if not isinstance(self.allowed_branches, frozenset):
            raise TypeError("policy branches must use an immutable frozenset")
        if not isinstance(self.allowed_repositories, frozenset):
            raise TypeError("policy repositories must use an immutable frozenset")
        if not isinstance(self.allowed_artifact_refs, frozenset):
            raise TypeError("policy artifact references must use an immutable frozenset")
        if not isinstance(self.allowed_runbooks, frozenset):
            raise TypeError("policy runbooks must use an immutable frozenset")
        if not isinstance(self.allowed_environments, frozenset):
            raise TypeError("policy environments must use an immutable frozenset")
        if self.allowed_network_destinations and not self.network_access:
            raise ValueError("network destinations require network_access=true")
        if self.allowed_tools - (_READ_TOOLS | _WRITE_TOOLS):
            raise ValueError("policy grant contains an unknown capability")
        if self.approved_scopes - _VALID_APPROVAL_SCOPES:
            raise ValueError("policy grant contains an unsupported approval scope")
        if self.allowed_environments - {"development", "eval", "staging", "production"}:
            raise ValueError("policy grant contains an unsupported deployment environment")
        for scope in self.write_scope:
            _validate_write_scope(scope)
        for destination in self.allowed_network_destinations:
            _normalize_destination(destination)
        for branch in self.allowed_branches:
            _validate_feature_branch(branch)
        for repository in self.allowed_repositories:
            _validate_repository_name(repository)
        if any(not isinstance(ref, str) or not ref for ref in self.allowed_artifact_refs):
            raise ValueError("policy artifact reference is invalid")
        if any(not isinstance(ref, str) or not ref for ref in self.allowed_runbooks):
            raise ValueError("policy runbook allowlist contains an invalid reference")


@dataclass(frozen=True)
class AgentExecutionGrant:
    tenant_id: str
    principal: str
    task_id: str
    policy_version: str
    allowed_tools: frozenset[str]
    write_scope: tuple[str, ...]
    network_access: bool
    allowed_network_destinations: frozenset[str]
    approval_scopes: frozenset[str]
    approval_ref: str | None
    allowed_branches: frozenset[str]
    allowed_repositories: frozenset[str]
    allowed_artifact_refs: frozenset[str]
    allowed_runbooks: frozenset[str]
    allowed_environments: frozenset[str]
    repository: str | None
    source_commit: str | None
    artifact_digest: str | None
    target_environment: str | None
    runbook_ref: str | None
    max_cost_usd: float
    max_tool_calls: int
    max_runtime_seconds: int


@dataclass(frozen=True)
class AgentExecutionContext:
    task: dict[str, Any]
    grant: AgentExecutionGrant
    tools: "ScopedToolGateway"


class DevOpsAgentHandler(Protocol):
    async def execute(self, context: AgentExecutionContext) -> dict[str, Any]: ...


# These are capability IDs from agent-task.schema.json. No generic shell,
# arbitrary network, protected-branch merge, secret-read, or unrestricted
# deployment capability is registered here.
_READ_TOOLS = frozenset(
    {
        "alerts.read",
        "artifact.read",
        "audit.read",
        "evidence.read",
        "health.read",
        "issue.read",
        "logs.read",
        "metrics.read",
        "policy.read",
        "release.read",
        "repository.read",
        "runbook.read",
        "sbom.read",
        "traces.read",
    }
)
_WRITE_TOOLS = frozenset(
    {
        "artifact.write",
        "build.execute",
        "deployment.execute",
        "documentation.write",
        "evidence.write",
        "incident.write",
        "manifest.write",
        "plan.write",
        "release.prepare",
        "repository.branch.write",
        "runbook.execute",
        "sbom.write",
        "task.create",
        "test.execute",
        "test.write",
    }
)
_PATH_SCOPED_TOOLS = frozenset(
    {
        "artifact.write",
        "documentation.write",
        "evidence.write",
        "manifest.write",
        "plan.write",
        "sbom.write",
        "test.write",
    }
)
_VALID_APPROVAL_SCOPES = frozenset(
    {
        "merge",
        "release",
        "deploy",
        "secret_change",
        "permission_change",
        "destructive_action",
        "production_data_change",
        "external_publication",
    }
)
def _validate_write_scope(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("write scope must contain 1..512 characters")
    if value.startswith("/") or "\\" in value:
        raise ValueError("write scope must be a relative POSIX path")
    parts = value.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("write scope contains an unsafe path segment")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise ValueError("write scope contains unsupported characters")
    return value.rstrip("/")


def _scope_covers(granted: str, requested: str) -> bool:
    grant = _validate_write_scope(granted)
    path = _validate_write_scope(requested)
    return path == grant or path.startswith(grant + "/")


def _validate_repository_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
        or any(part in {"", ".", ".."} or part.startswith(".") for part in value.split("/"))
    ):
        raise ValueError("repository must be an owner/name identifier")
    return value


def _validate_feature_branch(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or not value.startswith(("feature/", "feat/"))
        or "\\" in value
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Code Agent branches must be scoped feature/ or feat/ branches")
    return value


def _normalize_destination(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError("network destination must contain 1..255 characters")
    candidate = value if "://" in value else f"https://{value}"
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is not None
    ):
        raise ValueError("network destination must be a bare HTTPS hostname")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if not host or "*" in host or ".." in host:
        raise ValueError("network destination hostname is invalid")
    return host


_DEFAULT_PROFILES = (
    AgentProfile(
        "rusa_plan_agent",
        "plan",
        frozenset({"repository.read", "issue.read", "artifact.read", "task.create", "plan.write"}),
    ),
    AgentProfile(
        "rusa_code_agent",
        "code",
        frozenset({"repository.read", "issue.read", "artifact.read", "repository.branch.write", "test.write", "documentation.write"}),
        network_eligible=True,
    ),
    AgentProfile(
        "rusa_build_agent",
        "build",
        frozenset({"repository.read", "artifact.read", "build.execute", "artifact.write", "sbom.write"}),
        network_eligible=True,
    ),
    AgentProfile(
        "rusa_test_agent",
        "test",
        frozenset({"repository.read", "artifact.read", "test.execute", "evidence.write", "metrics.read"}),
    ),
    AgentProfile(
        "rusa_release_agent",
        "release",
        frozenset({"artifact.read", "evidence.read", "release.prepare", "manifest.write", "sbom.read"}),
    ),
    AgentProfile(
        "rusa_deploy_agent",
        "deploy",
        frozenset({"artifact.read", "release.read", "deployment.execute", "health.read", "runbook.read"}),
        network_eligible=True,
    ),
    AgentProfile(
        "rusa_operate_agent",
        "operate",
        frozenset({"metrics.read", "logs.read", "runbook.read", "runbook.execute", "incident.write"}),
        network_eligible=True,
    ),
    AgentProfile(
        "rusa_monitor_agent",
        "monitor",
        frozenset({"metrics.read", "logs.read", "traces.read", "alerts.read"}),
        read_only=True,
    ),
    AgentProfile(
        "rusa_security_guard_agent",
        None,
        frozenset({"repository.read", "issue.read", "artifact.read", "evidence.read", "policy.read", "audit.read", "metrics.read", "logs.read", "traces.read"}),
        read_only=True,
    ),
)


class DevOpsAgentRegistry:
    """Immutable stage authority plus explicitly registered execution handlers."""

    def __init__(self, profiles: tuple[AgentProfile, ...] = _DEFAULT_PROFILES):
        by_id = {profile.agent_id: profile for profile in profiles}
        baseline = {profile.agent_id: profile for profile in _DEFAULT_PROFILES}
        if len(by_id) != len(profiles):
            raise ValueError("agent registry contains duplicate agent identities")
        if set(by_id) != set(baseline):
            raise ValueError("the DevOps registry must define all eight stages and Security Guard")
        for agent_id, profile in by_id.items():
            approved = baseline[agent_id]
            if profile.stage != approved.stage:
                raise ValueError(f"{agent_id} stage assignment cannot be changed")
            if profile.allowed_tools - approved.allowed_tools:
                raise ValueError(f"{agent_id} profile cannot expand registered capabilities")
            if approved.read_only and not profile.read_only:
                raise ValueError(f"{agent_id} read-only status cannot be relaxed")
            if profile.network_eligible and not approved.network_eligible:
                raise ValueError(f"{agent_id} network eligibility cannot be expanded")
            if profile.allowed_tools - (_READ_TOOLS | _WRITE_TOOLS):
                raise ValueError(f"{agent_id} has an unknown capability")
            if profile.read_only and profile.allowed_tools - _READ_TOOLS:
                raise ValueError(f"{agent_id} is read-only but has a write capability")
        self._profiles = by_id
        self._handlers: dict[str, DevOpsAgentHandler] = {}

    @property
    def profiles(self) -> tuple[AgentProfile, ...]:
        return tuple(self._profiles[key] for key in sorted(self._profiles))

    def profile_for(self, agent_id: str) -> AgentProfile:
        try:
            return self._profiles[agent_id]
        except KeyError as exc:
            raise AgentAuthorizationError("agent identity is not registered") from exc

    def register(self, agent_id: str, handler: DevOpsAgentHandler) -> None:
        self.profile_for(agent_id)
        if not callable(getattr(handler, "execute", None)):
            raise TypeError("agent handler must implement async execute(context)")
        if agent_id in self._handlers:
            raise ValueError(f"handler already registered for {agent_id}")
        self._handlers[agent_id] = handler

    def handler_for(self, agent_id: str) -> DevOpsAgentHandler:
        try:
            return self._handlers[agent_id]
        except KeyError as exc:
            raise AgentHandlerNotRegisteredError(
                f"no execution handler is registered for {agent_id}"
            ) from exc

    def authorize(
        self,
        task: dict[str, Any],
        *,
        authenticated_tenant_id: str,
        authenticated_principal: str,
        policy_grant: ExecutionPolicyGrant,
    ) -> AgentExecutionGrant:
        validate_agent_task(task)
        if task["tenant_id"] != authenticated_tenant_id:
            raise AgentAuthorizationError("task tenant does not match authenticated workload")
        if policy_grant.tenant_id != authenticated_tenant_id:
            raise AgentAuthorizationError("policy grant tenant does not match authenticated workload")
        if task["agent_id"] != authenticated_principal:
            raise AgentAuthorizationError("task is not assigned to the authenticated workload")
        if policy_grant.principal != authenticated_principal:
            raise AgentAuthorizationError("policy grant principal does not match authenticated workload")

        profile = self.profile_for(authenticated_principal)
        if profile.stage is not None and task["stage"] != profile.stage:
            raise AgentAuthorizationError("task stage does not match registered agent identity")
        if task["audit"]["policy_version"] != policy_grant.policy_version:
            raise AgentAuthorizationError("task policy version is not the active policy grant")

        constraints = task["constraints"]
        requested_tools = frozenset(constraints["allowed_tools"])
        outside_profile = requested_tools - profile.allowed_tools
        if outside_profile:
            raise AgentCapabilityDeniedError(
                "task requests capabilities outside the agent registry: "
                + ", ".join(sorted(outside_profile))
            )
        outside_policy = requested_tools - policy_grant.allowed_tools
        if outside_policy:
            raise AgentCapabilityDeniedError(
                "task requests capabilities outside the Policy Engine grant: "
                + ", ".join(sorted(outside_policy))
            )

        write_scope = tuple(_validate_write_scope(item) for item in constraints.get("write_scope", []))
        if profile.read_only and (write_scope or requested_tools & _WRITE_TOOLS):
            raise AgentCapabilityDeniedError("read-only agent cannot receive write capabilities or scope")
        if write_scope:
            if not policy_grant.write_scope or any(
                not any(_scope_covers(granted, requested) for granted in policy_grant.write_scope)
                for requested in write_scope
            ):
                raise AgentCapabilityDeniedError("task write scope exceeds the Policy Engine grant")
        if requested_tools & _PATH_SCOPED_TOOLS and not write_scope:
            raise AgentCapabilityDeniedError("path-writing capabilities require an explicit write scope")
        if "repository.branch.write" in requested_tools and not policy_grant.allowed_branches:
            raise AgentCapabilityDeniedError("branch-write capability requires a Policy Engine feature-branch allowlist")
        if "runbook.execute" in requested_tools:
            runbook_ref = task.get("input", {}).get("runbook_ref")
            if not runbook_ref or runbook_ref not in policy_grant.allowed_runbooks:
                raise AgentCapabilityDeniedError("runbook execution is outside the Policy Engine allowlist")
        task_input = task.get("input", {})
        repository = task_input.get("repository")
        if repository is not None:
            _validate_repository_name(repository)
            if repository not in policy_grant.allowed_repositories:
                raise AgentCapabilityDeniedError("repository is outside the Policy Engine resource grant")
        if requested_tools & {"repository.read", "repository.branch.write"} and not repository:
            raise AgentCapabilityDeniedError("repository tools require a task-bound repository")
        artifact_refs = set()
        for key, value in task_input.items():
            if key.endswith("_ref") and isinstance(value, str):
                artifact_refs.add(value)
            elif key == "evidence_refs" and isinstance(value, list):
                artifact_refs.update(item for item in value if isinstance(item, str))
        if not artifact_refs.issubset(policy_grant.allowed_artifact_refs):
            raise AgentCapabilityDeniedError("task artifact references exceed the Policy Engine resource grant")

        target_environment = task_input.get("environment")
        if task["stage"] == "deploy" and target_environment not in policy_grant.allowed_environments:
            raise AgentCapabilityDeniedError("deployment environment is outside the Policy Engine grant")

        network_access = constraints.get("network_access", False)
        task_destinations = frozenset(
            _normalize_destination(item)
            for item in constraints.get("allowed_network_destinations", [])
        )
        if network_access:
            if profile.read_only or not profile.network_eligible:
                raise AgentCapabilityDeniedError("this agent is not eligible for network access")
            if not policy_grant.network_access:
                raise AgentCapabilityDeniedError("Policy Engine did not grant network access")
            policy_destinations = frozenset(
                _normalize_destination(item)
                for item in policy_grant.allowed_network_destinations
            )
            if not task_destinations or not task_destinations.issubset(policy_destinations):
                raise AgentCapabilityDeniedError(
                    "task network destinations exceed or omit the Policy Engine allowlist"
                )
        elif task_destinations:
            raise AgentCapabilityDeniedError("network destinations are invalid when network_access is false")

        budget = constraints["budget"]
        if (
            isinstance(budget["max_cost_usd"], bool)
            or not isinstance(budget["max_cost_usd"], (int, float))
            or not math.isfinite(budget["max_cost_usd"])
            or isinstance(budget["max_tool_calls"], bool)
            or not isinstance(budget["max_tool_calls"], int)
            or isinstance(budget["max_runtime_seconds"], bool)
            or not isinstance(budget["max_runtime_seconds"], int)
        ):
            raise AgentBudgetExceededError("task budget contains an invalid numeric value")
        if (
            budget["max_cost_usd"] > policy_grant.max_cost_usd
            or budget["max_tool_calls"] > policy_grant.max_tool_calls
            or budget["max_runtime_seconds"] > policy_grant.max_runtime_seconds
        ):
            raise AgentBudgetExceededError("task budget exceeds the Policy Engine grant")

        approval_scopes = frozenset(constraints.get("approval_scope", []))
        if approval_scopes - _VALID_APPROVAL_SCOPES:
            raise AgentApprovalRequiredError("task contains an unsupported approval scope")
        approval_ref = constraints.get("approval_ref")
        requires_deploy_approval = (
            task["stage"] == "deploy"
            and task.get("input", {}).get("environment") == "production"
        )
        requires_protected_approval = (
            requires_deploy_approval
            or bool(constraints.get("approval_required"))
            or bool(approval_scopes & {"merge", "release", "deploy", "secret_change", "permission_change", "destructive_action", "production_data_change", "external_publication"})
        )
        if requires_deploy_approval and "deploy" not in approval_scopes:
            raise AgentApprovalRequiredError("production deployment requires deploy approval scope")
        if requires_protected_approval:
            if not approval_ref or approval_ref not in policy_grant.approved_refs:
                raise AgentApprovalRequiredError("a current Policy Engine approval reference is required")
            if not approval_scopes or not approval_scopes.issubset(policy_grant.approved_scopes):
                raise AgentApprovalRequiredError("approval scope is not covered by the Policy Engine grant")

        return AgentExecutionGrant(
            tenant_id=authenticated_tenant_id,
            principal=authenticated_principal,
            task_id=task["task_id"],
            policy_version=policy_grant.policy_version,
            allowed_tools=requested_tools,
            write_scope=write_scope,
            network_access=network_access,
            allowed_network_destinations=task_destinations,
            approval_scopes=approval_scopes,
            approval_ref=approval_ref,
            allowed_branches=policy_grant.allowed_branches,
            allowed_repositories=policy_grant.allowed_repositories,
            allowed_artifact_refs=frozenset(artifact_refs),
            allowed_runbooks=policy_grant.allowed_runbooks,
            allowed_environments=policy_grant.allowed_environments,
            repository=repository,
            source_commit=task_input.get("commit_sha"),
            artifact_digest=task_input.get("artifact_digest"),
            target_environment=target_environment,
            runbook_ref=task_input.get("runbook_ref"),
            max_cost_usd=budget["max_cost_usd"],
            max_tool_calls=budget["max_tool_calls"],
            max_runtime_seconds=budget["max_runtime_seconds"],
        )


class ScopedToolGateway:
    """Only exposes tools and resources covered by an immutable execution grant."""

    def __init__(self, delegate: ToolInvoker, grant: AgentExecutionGrant):
        if not callable(getattr(delegate, "invoke", None)):
            raise TypeError("tool delegate must implement invoke(tool_id, arguments)")
        self._delegate = delegate
        self._grant = grant
        self._tool_calls = 0

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    async def invoke(self, tool_id: str, arguments: dict[str, Any]) -> Any:
        if tool_id not in self._grant.allowed_tools:
            raise ToolInvocationDeniedError("tool is outside the task execution grant")
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be a JSON object")
        if self._tool_calls >= self._grant.max_tool_calls:
            raise AgentBudgetExceededError("task tool-call budget is exhausted")

        if tool_id in _PATH_SCOPED_TOOLS:
            path = arguments.get("path") or arguments.get("resource_path") or arguments.get("write_path")
            if not isinstance(path, str):
                raise ToolInvocationDeniedError("path-scoped tool call requires a path")
            normalized = _validate_write_scope(path)
            if not any(_scope_covers(scope, normalized) for scope in self._grant.write_scope):
                raise ToolInvocationDeniedError("tool path is outside the task write scope")

        destination_values = [
            arguments[key]
            for key in ("destination", "url", "host", "hostname", "endpoint")
            if arguments.get(key) is not None
        ]
        for destination_value in destination_values:
            if not self._grant.network_access:
                raise ToolInvocationDeniedError("network access is not granted")
            destination = _normalize_destination(destination_value)
            if destination not in self._grant.allowed_network_destinations:
                raise ToolInvocationDeniedError("network destination is outside the task allowlist")

        if tool_id in {"repository.read", "repository.branch.write"}:
            if arguments.get("repository") != self._grant.repository:
                raise ToolInvocationDeniedError("repository is outside the task resource grant")
        if tool_id == "repository.branch.write":
            branch = arguments.get("branch") or arguments.get("work_branch")
            if branch not in self._grant.allowed_branches:
                raise ToolInvocationDeniedError("branch is outside the Policy Engine feature-branch allowlist")
            _validate_feature_branch(branch)
        if tool_id in {"artifact.read", "evidence.read", "sbom.read", "release.read"}:
            resource_ref = (
                arguments.get("resource_ref")
                or arguments.get("artifact_ref")
                or arguments.get("ref")
            )
            if resource_ref not in self._grant.allowed_artifact_refs:
                raise ToolInvocationDeniedError("artifact reference is outside the task resource grant")
        if tool_id == "runbook.read" and arguments.get("runbook_ref") != self._grant.runbook_ref:
            raise ToolInvocationDeniedError("runbook is outside the task grant")
        if tool_id == "build.execute":
            if arguments.get("commit_sha") != self._grant.source_commit:
                raise ToolInvocationDeniedError("build source commit does not match the task")
        if tool_id == "test.execute":
            if (
                arguments.get("commit_sha") != self._grant.source_commit
                or arguments.get("artifact_digest") != self._grant.artifact_digest
            ):
                raise ToolInvocationDeniedError("test must target the exact task commit and artifact digest")
        if tool_id == "release.prepare":
            if (
                arguments.get("commit_sha") != self._grant.source_commit
                or arguments.get("artifact_digest") != self._grant.artifact_digest
                or arguments.get("test_evidence_ref") not in self._grant.allowed_artifact_refs
                or arguments.get("sbom_ref") not in self._grant.allowed_artifact_refs
            ):
                raise ToolInvocationDeniedError("release candidate evidence does not match the task")
        if tool_id == "deployment.execute":
            environment = arguments.get("environment")
            if (
                environment != self._grant.target_environment
                or environment not in self._grant.allowed_environments
                or arguments.get("release_manifest_ref") not in self._grant.allowed_artifact_refs
                or arguments.get("commit_sha") != self._grant.source_commit
                or arguments.get("artifact_digest") != self._grant.artifact_digest
            ):
                raise ToolInvocationDeniedError("deployment artifact, commit, or environment is outside the task grant")
            if environment == "production":
                approval_ref = arguments.get("approval_ref")
                if (
                    "deploy" not in self._grant.approval_scopes
                    or approval_ref != self._grant.approval_ref
                ):
                    raise AgentApprovalRequiredError("production deployment approval is missing")
        if tool_id == "runbook.execute":
            if arguments.get("runbook_ref") != self._grant.runbook_ref:
                raise ToolInvocationDeniedError("runbook is outside the task grant")
            if arguments.get("destructive") is True:
                approval_ref = arguments.get("approval_ref")
                if (
                    "destructive_action" not in self._grant.approval_scopes
                    or approval_ref != self._grant.approval_ref
                ):
                    raise AgentApprovalRequiredError("destructive runbook approval is missing")

        self._tool_calls += 1
        result = self._delegate.invoke(tool_id, arguments)
        if inspect.isawaitable(result):
            return await result
        return result


class DevOpsAgentRuntime:
    """Executes a registered specialist behind task, identity, and policy gates."""

    def __init__(self, registry: DevOpsAgentRegistry):
        self.registry = registry

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_tenant_id: str,
        authenticated_principal: str,
        policy_grant: ExecutionPolicyGrant,
        tool_delegate: ToolInvoker,
    ) -> dict[str, Any]:
        task_snapshot = copy.deepcopy(task)
        grant = self.registry.authorize(
            task_snapshot,
            authenticated_tenant_id=authenticated_tenant_id,
            authenticated_principal=authenticated_principal,
            policy_grant=policy_grant,
        )
        handler = self.registry.handler_for(authenticated_principal)
        gateway = ScopedToolGateway(tool_delegate, grant)
        context = AgentExecutionContext(
            task=copy.deepcopy(task_snapshot),
            grant=grant,
            tools=gateway,
        )
        operation = handler.execute(context)
        if not inspect.isawaitable(operation):
            raise TypeError("registered agent handlers must implement async execute(context)")

        timeout_seconds = float(grant.max_runtime_seconds)
        deadline_value = task_snapshot.get("deadline_at")
        if deadline_value:
            deadline = datetime.fromisoformat(deadline_value.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            remaining = (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise AgentBudgetExceededError("task deadline has expired")
            timeout_seconds = min(timeout_seconds, remaining)

        try:
            result = await asyncio.wait_for(operation, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise AgentBudgetExceededError("agent execution exceeded its task runtime budget") from exc
        validate_agent_result_for_task(task_snapshot, result)
        return result
