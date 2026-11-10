"""Versioned DevOps agent contracts, identities, and deterministic permissions.

This is a contract foundation only. A registered profile does not imply that an
agent executor or its external tools are deployed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import jsonschema


_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "devops"
_SCHEMA_FILES = {
    "task": "agent-task.schema.json",
    "result": "agent-result.schema.json",
    "event": "workflow-event.schema.json",
}


class ContractValidationError(ValueError):
    """Raised when a versioned DevOps contract is invalid."""


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    display_name: str
    stage: str | None
    capabilities: frozenset[str]
    allowed_tools: frozenset[str]
    allowed_actions: frozenset[str]
    read_scopes: frozenset[str]
    write_scopes: frozenset[str]
    network_access: bool
    runtime_status: str


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    requires_human_approval: bool
    reason: str


PROTECTED_ACTIONS = frozenset({
    "merge_protected_branch",
    "branch_protection.change",
    "deploy_staging",
    "deploy_production",
    "database_migration.risky",
    "resource.delete",
    "secret.change",
    "identity.change",
    "security_policy.change",
    "budget.override",
    "social.publish_private_asset",
})

_READ = frozenset({"repository.read"})
_PLAN_TOOLS = frozenset({"repository.read", "plan.write", "task.create"})
_CODE_TOOLS = frozenset({"repository.read", "feature_branch.write", "pull_request.draft.create"})
_BUILD_TOOLS = frozenset({"source.read", "container.build", "artifact.write"})
_TEST_TOOLS = frozenset({"source.read", "artifact.read", "test.execute", "evidence.write"})
_RELEASE_TOOLS = frozenset({"evidence.read", "manifest.write", "release_candidate.create"})
_DEPLOY_TOOLS = frozenset({"deployment.status.read", "health_check.execute", "rollback.plan"})
_OPERATE_TOOLS = frozenset({"metrics.read", "logs.read", "runbook.read", "runbook.execute.safe"})
_MONITOR_TOOLS = frozenset({"metrics.read", "logs.read", "traces.read", "alert.create"})
_SECURITY_TOOLS = frozenset({"security.scan", "audit.read", "workflow.block"})
_ORCHESTRATOR_TOOLS = frozenset({"workflow.read", "task.create", "task.dispatch", "workflow.cancel"})


def _profile(
    agent_id: str,
    display_name: str,
    *,
    stage: str | None,
    tools: frozenset[str],
    actions: frozenset[str],
    read_scopes: frozenset[str] = _READ,
    write_scopes: frozenset[str] = frozenset(),
    network_access: bool = False,
    capabilities: frozenset[str] = frozenset(),
    runtime_status: str = "contract_only",
) -> AgentProfile:
    return AgentProfile(
        agent_id=agent_id,
        display_name=display_name,
        stage=stage,
        capabilities=capabilities,
        allowed_tools=tools,
        allowed_actions=actions,
        read_scopes=read_scopes,
        write_scopes=write_scopes,
        network_access=network_access,
        runtime_status=runtime_status,
    )


_profiles = {
    "supreme_orchestrator": _profile(
        "supreme_orchestrator", "Supreme Orchestrator", stage=None,
        tools=_ORCHESTRATOR_TOOLS, actions=frozenset({"workflow.read", "task.create", "task.dispatch", "workflow.cancel"}),
        read_scopes=frozenset({"workflow.state", "agent.registry", "task.results"}),
        write_scopes=frozenset({"workflow.state", "task.queue"}), capabilities=frozenset({"orchestration"}),
        runtime_status="partial",
    ),
    "system_architect": _profile(
        "system_architect", "System Architect", stage=None,
        tools=frozenset({"repository.read", "architecture.write", "plan.read"}),
        actions=frozenset({"repository.read", "architecture.write"}),
        write_scopes=frozenset({"architecture-docs/"}), capabilities=frozenset({"architecture"}),
        runtime_status="partial",
    ),
    "planner": _profile(
        "planner", "Planner", stage=None, tools=_PLAN_TOOLS,
        actions=frozenset({"repository.read", "plan.create", "task.create"}),
        write_scopes=frozenset({"plans/", "backlog/", "acceptance-criteria/"}),
        capabilities=frozenset({"planning"}), runtime_status="partial",
    ),
    "task_decomposer": _profile(
        "task_decomposer", "Task Decomposer", stage=None,
        tools=frozenset({"plan.read", "task.create", "dag.validate"}),
        actions=frozenset({"plan.read", "task.create", "dag.validate"}),
        read_scopes=frozenset({"plans/", "workflow.state"}),
        write_scopes=frozenset({"task.graph/"}), capabilities=frozenset({"task_decomposition"}),
    ),
    "software_engineer": _profile(
        "software_engineer", "Software Engineer", stage=None, tools=_CODE_TOOLS,
        actions=frozenset({"repository.read", "feature_branch.write", "pull_request.draft.create"}),
        write_scopes=frozenset({"feature-branches/"}), network_access=True,
        capabilities=frozenset({"implementation"}), runtime_status="partial",
    ),
    "devops": _profile(
        "devops", "DevOps", stage=None,
        tools=_BUILD_TOOLS | _DEPLOY_TOOLS,
        actions=frozenset({"source.read", "container.build", "artifact.write", "deployment.status.read"}),
        read_scopes=frozenset({"source/", "release-manifests/", "deployment-records/"}),
        write_scopes=frozenset({"build-artifacts/"}), network_access=True,
        capabilities=frozenset({"build", "deployment_operations"}),
    ),
    "qa_testing": _profile(
        "qa_testing", "QA & Testing", stage=None, tools=_TEST_TOOLS,
        actions=frozenset({"source.read", "artifact.read", "test.execute", "evidence.write"}),
        read_scopes=frozenset({"source/", "build-artifacts/"}),
        write_scopes=frozenset({"test-reports/"}), capabilities=frozenset({"quality_assurance"}),
        runtime_status="partial",
    ),
    "security_policy": _profile(
        "security_policy", "Security & Policy", stage=None, tools=_SECURITY_TOOLS,
        actions=frozenset({"security.scan", "audit.read", "workflow.block"}),
        read_scopes=frozenset({"source/", "workflow.state", "audit/"}),
        write_scopes=frozenset({"security-findings/"}), capabilities=frozenset({"security_governance"}),
        runtime_status="partial",
    ),
    "memory_knowledge": _profile(
        "memory_knowledge", "Memory & Knowledge", stage=None,
        tools=frozenset({"memory.read", "memory.write_scoped", "knowledge.search"}),
        actions=frozenset({"memory.read", "memory.write_scoped", "knowledge.search"}),
        read_scopes=frozenset({"tenant-memory/", "knowledge-base/"}),
        write_scopes=frozenset({"tenant-memory/"}), capabilities=frozenset({"memory", "knowledge"}),
        runtime_status="partial",
    ),
    "evaluation_reporting": _profile(
        "evaluation_reporting", "Evaluation & Reporting", stage=None,
        tools=frozenset({"evidence.read", "metrics.read", "report.create"}),
        actions=frozenset({"evidence.read", "metrics.read", "report.create"}),
        read_scopes=frozenset({"evidence/", "metrics/", "audit/"}),
        write_scopes=frozenset({"reports/"}), capabilities=frozenset({"evaluation", "reporting"}),
        runtime_status="partial",
    ),
    "rusa_devops_orchestrator": _profile(
        "rusa_devops_orchestrator", "RUSA DevOps Orchestrator", stage=None,
        tools=_ORCHESTRATOR_TOOLS, actions=frozenset({"workflow.read", "task.create", "task.dispatch", "workflow.cancel"}),
        read_scopes=frozenset({"workflow.state", "agent.registry", "task.results"}),
        write_scopes=frozenset({"workflow.state", "task.queue"}), network_access=False,
        capabilities=frozenset({"devops_orchestration"}),
    ),
    "rusa_plan_agent": _profile(
        "rusa_plan_agent", "RUSA Plan Agent", stage="plan", tools=_PLAN_TOOLS,
        actions=frozenset({"repository.read", "plan.create", "task.create"}),
        write_scopes=frozenset({"plans/", "backlog/", "acceptance-criteria/"}),
        capabilities=frozenset({"requirements", "planning"}), network_access=True,
        runtime_status="partial",
    ),
    "rusa_code_agent": _profile(
        "rusa_code_agent", "RUSA Code Agent", stage="code", tools=_CODE_TOOLS,
        actions=frozenset({"repository.read", "feature_branch.write", "pull_request.draft.create"}),
        write_scopes=frozenset({"feature-branches/"}), network_access=True,
        capabilities=frozenset({"implementation"}), runtime_status="partial",
    ),
    "rusa_build_agent": _profile(
        "rusa_build_agent", "RUSA Build Agent", stage="build", tools=_BUILD_TOOLS,
        actions=frozenset({"source.read", "container.build", "artifact.write"}),
        read_scopes=frozenset({"source/"}), write_scopes=frozenset({"build-artifacts/"}),
        capabilities=frozenset({"reproducible_build"}),
    ),
    "rusa_test_agent": _profile(
        "rusa_test_agent", "RUSA Test Agent", stage="test", tools=_TEST_TOOLS,
        actions=frozenset({"source.read", "artifact.read", "test.execute", "evidence.write"}),
        read_scopes=frozenset({"source/", "build-artifacts/"}), write_scopes=frozenset({"test-reports/"}),
        capabilities=frozenset({"testing", "quality_evidence"}), runtime_status="partial",
    ),
    "rusa_release_agent": _profile(
        "rusa_release_agent", "RUSA Release Agent", stage="release", tools=_RELEASE_TOOLS,
        actions=frozenset({"evidence.read", "manifest.write", "release_candidate.create"}),
        read_scopes=frozenset({"evidence/", "build-artifacts/"}),
        write_scopes=frozenset({"release-candidates/", "release-manifests/"}),
        capabilities=frozenset({"release_readiness"}),
    ),
    "rusa_deploy_agent": _profile(
        "rusa_deploy_agent", "RUSA Deploy Agent", stage="deploy", tools=_DEPLOY_TOOLS,
        actions=frozenset({"deployment.status.read", "health_check.execute", "rollback.plan"}),
        read_scopes=frozenset({"release-manifests/", "deployment-records/"}),
        write_scopes=frozenset({"deployment-records/"}), network_access=True,
        capabilities=frozenset({"deployment", "health_verification"}),
    ),
    "rusa_operate_agent": _profile(
        "rusa_operate_agent", "RUSA Operate Agent", stage="operate", tools=_OPERATE_TOOLS,
        actions=frozenset({"metrics.read", "logs.read", "runbook.read", "runbook.execute.safe"}),
        read_scopes=frozenset({"metrics/", "logs/", "runbooks/"}),
        write_scopes=frozenset({"incident-records/", "runbook-records/"}), network_access=True,
        capabilities=frozenset({"operations", "incident_response"}),
    ),
    "rusa_monitor_agent": _profile(
        "rusa_monitor_agent", "RUSA Monitor Agent", stage="monitor", tools=_MONITOR_TOOLS,
        actions=frozenset({"metrics.read", "logs.read", "traces.read", "alert.create"}),
        read_scopes=frozenset({"metrics/", "logs/", "traces/", "slo/"}),
        write_scopes=frozenset({"alerts/", "monitor-feedback/"}), network_access=True,
        capabilities=frozenset({"observability", "slo_monitoring"}),
    ),
    "rusa_security_guard_agent": _profile(
        "rusa_security_guard_agent", "RUSA Security Guard Agent", stage=None, tools=_SECURITY_TOOLS,
        actions=frozenset({"security.scan", "audit.read", "workflow.block"}),
        read_scopes=frozenset({"source/", "workflow.state", "audit/", "evidence/"}),
        write_scopes=frozenset({"security-findings/"}), capabilities=frozenset({"cross_stage_security"}),
        runtime_status="partial",
    ),
}
AGENT_PROFILES = MappingProxyType(_profiles)


@lru_cache(maxsize=len(_SCHEMA_FILES))
def _load_schema(kind: str) -> dict[str, Any]:
    try:
        filename = _SCHEMA_FILES[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown DevOps contract kind: {kind}") from exc
    path = _SCHEMA_DIR / filename
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load DevOps {kind} schema") from exc
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def _validate(kind: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ContractValidationError(f"{kind} contract must be a JSON object")
    validator = jsonschema.Draft202012Validator(
        _load_schema(kind), format_checker=jsonschema.FormatChecker()
    )
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(map(str, error.absolute_path)))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ContractValidationError(f"Invalid {kind} contract at {location}: {error.message}")


def get_agent_profile(agent_id: str) -> AgentProfile:
    try:
        return AGENT_PROFILES[agent_id]
    except KeyError as exc:
        raise ContractValidationError(f"Unknown agent identity: {agent_id}") from exc


def authorize_agent_action(agent_id: str, action: str) -> AuthorizationDecision:
    profile = get_agent_profile(agent_id)
    if action in PROTECTED_ACTIONS:
        return AuthorizationDecision(
            False, True, f"Human approval is required for protected action '{action}'"
        )
    if action not in profile.allowed_actions:
        return AuthorizationDecision(
            False, False, f"Action '{action}' is outside the {agent_id} permission set"
        )
    return AuthorizationDecision(True, False, "Allowed by the registered agent profile")


def validate_agent_task(task: dict[str, Any]) -> None:
    _validate("task", task)
    profile = get_agent_profile(task["agent_id"])
    if profile.stage is not None and profile.stage != task["stage"]:
        raise ContractValidationError(
            f"Agent {profile.agent_id} cannot execute stage '{task['stage']}'"
        )
    constraints = task["constraints"]
    if not set(constraints["allowed_tools"]).issubset(profile.allowed_tools):
        raise ContractValidationError("Task requests tools outside the agent allowlist")
    if not set(constraints["write_scope"]).issubset(profile.write_scopes):
        raise ContractValidationError("Task requests write scope outside the agent permission set")
    if constraints["network_access"] and not profile.network_access:
        raise ContractValidationError("Task requests network access outside the agent permission set")
    for action in constraints.get("requested_actions", []):
        decision = authorize_agent_action(profile.agent_id, action)
        if decision.requires_human_approval and not constraints["approval_required"]:
            raise ContractValidationError(
                f"Protected action '{action}' must be marked approval_required"
            )
        if not decision.allowed and not decision.requires_human_approval:
            raise ContractValidationError(decision.reason)
    if task["attempt"] > task["max_attempts"]:
        raise ContractValidationError("Task attempt cannot exceed max_attempts")
    created_at = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
    deadline_at = datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00"))
    if created_at.tzinfo is None or deadline_at.tzinfo is None or deadline_at <= created_at:
        raise ContractValidationError("Task deadline must be timezone-aware and after created_at")


def validate_agent_result(result: dict[str, Any], task: dict[str, Any] | None = None) -> None:
    _validate("result", result)
    profile = get_agent_profile(result["agent_id"])
    if profile.stage is not None and profile.stage != result["stage"]:
        raise ContractValidationError(
            f"Agent {profile.agent_id} cannot report stage '{result['stage']}'"
        )
    if task is not None:
        for field in ("task_id", "workflow_id", "tenant_id", "agent_id", "stage", "attempt"):
            if result[field] != task[field]:
                raise ContractValidationError(f"Agent result does not match task field '{field}'")


def validate_workflow_event(event: dict[str, Any]) -> None:
    _validate("event", event)
    if event["workflow_id"] != event["run_id"]:
        raise ContractValidationError("workflow_id and ADR run_id must identify the same workflow")


def validate_workflow_handoff(
    task: dict[str, Any], result: dict[str, Any], event: dict[str, Any]
) -> None:
    validate_agent_task(task)
    validate_agent_result(result, task)
    validate_workflow_event(event)
    for field in ("tenant_id", "workflow_id", "task_id", "attempt", "correlation_id"):
        expected = task[field] if field in task else task["workflow_id"]
        if event.get(field) != expected:
            raise ContractValidationError(f"Workflow event does not match task field '{field}'")
    if event["producer"] != task["agent_id"]:
        raise ContractValidationError("Workflow event producer does not match the assigned agent")
    expected_events = {
        "plan": {"plan.completed"},
        "code": {"code.completed"},
        "build": {"build.completed"},
        "test": {"test.completed"},
        "release": {"release.approved"},
        "deploy": {"deploy.completed"},
        "operate": {"operate.incident_detected", "operate.recovered"},
        "monitor": {"monitor.alert_triggered", "monitor.feedback_created"},
        "security": {"policy.violation", "workflow.blocked"},
    }
    allowed_events = (
        expected_events[task["stage"]]
        if result["status"] == "passed"
        else {"workflow.blocked"}
    )
    if event["type"] not in allowed_events:
        raise ContractValidationError("Workflow event type does not match the agent result status")

    artifact_refs = {item["ref"] for item in result["artifacts"]}
    artifact_digests = {item["digest"] for item in result["artifacts"]}
    evidence_refs = {item["ref"] for item in result["evidence"]}
    payload = event["payload"]
    if event["type"] in {"plan.completed", "code.completed", "build.completed", "test.completed"}:
        if payload.get("stage") != task["stage"] or payload.get("status") != "passed":
            raise ContractValidationError("Workflow event stage/status does not match the result")
        if payload.get("result_ref") not in artifact_refs:
            raise ContractValidationError("Workflow event result_ref is not bound to a result artifact")
        if not set(payload.get("evidence_refs", [])).issubset(evidence_refs):
            raise ContractValidationError("Workflow event evidence refs are not bound to result evidence")
    elif event["type"] == "release.approved":
        if payload.get("release_ref") not in artifact_refs:
            raise ContractValidationError("Release event does not reference a result artifact")
        if payload.get("approval_ref") not in evidence_refs:
            raise ContractValidationError("Release approval is not bound to result evidence")
        if payload.get("manifest_digest") not in artifact_digests:
            raise ContractValidationError("Release manifest digest does not match a result artifact")
    elif event["type"] == "deploy.completed":
        if payload.get("deployment_ref") not in artifact_refs:
            raise ContractValidationError("Deployment event does not reference a result artifact")
        if payload.get("artifact_digest") not in artifact_digests:
            raise ContractValidationError("Deployment digest does not match a result artifact")
        if payload.get("health_status") != "healthy":
            raise ContractValidationError("A passed deployment requires a healthy status")
    elif event["type"] in {"operate.incident_detected", "operate.recovered"}:
        refs = {payload.get("incident_ref"), payload.get("recovery_ref")} - {None}
        if not refs.issubset(artifact_refs | evidence_refs):
            raise ContractValidationError("Operations event references are not bound to result evidence")
    elif event["type"] == "monitor.alert_triggered":
        if payload.get("alert_ref") not in artifact_refs | evidence_refs:
            raise ContractValidationError("Monitor alert is not bound to result evidence")
    elif event["type"] == "monitor.feedback_created":
        refs = {payload.get("feedback_ref"), payload.get("source_ref")} - {None}
        if not refs.issubset(artifact_refs | evidence_refs):
            raise ContractValidationError("Monitor feedback references are not bound to result evidence")
