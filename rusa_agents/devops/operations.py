"""Allowlisted Operate runbooks and read-only Monitor telemetry specialists.

Host adapters own privileged service access. Agent task data selects only a
registered runbook/action or a read-only telemetry window; it never supplies
shell commands, endpoints, credentials, or deployment destinations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

import jsonschema

from .agent_registry import MONITOR_AGENT, OPERATE_AGENT, PLAN_AGENT, authorize_agent_task
from .artifacts import ContentAddressedArtifactStore
from .specialists import (
    ExecutionLease,
    ExecutionLeaseError,
    SpecialistOutcome,
    _checkpoint,
    _failure,
    _now,
    _result,
    _timeout_seconds,
    _timestamp,
)

_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_SHA256_REF_RE = re.compile(r"^(?:artifact|evidence)://sha256/[a-f0-9]{64}$")
_SECRET_KEYS = frozenset(
    {
        "api_key", "apikey", "access_token", "accesstoken", "refresh_token",
        "refreshtoken", "authorization", "password", "client_secret",
        "clientsecret", "private_key", "privatekey", "secret", "secret_key",
        "secretkey", "bearer_token", "bearertoken", "credential", "credentials",
        "token",
    }
)
_APPROVAL_SCOPES = frozenset(
    {
        "merge", "release", "deploy", "secret_change", "permission_change",
        "destructive_action", "production_data_change", "external_publication",
    }
)


class OperationsPolicyError(PermissionError):
    """An operations request is outside its registered runbook or policy."""


def _safe_payload(value: Any, *, path: str = "payload", max_bytes: int = 64_000) -> None:
    """Reject credential-shaped fields and unbounded/non-JSON adapter output."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"adapter output keys must be strings at {path}")
            normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in _SECRET_KEYS or normalized.replace("_", "") in _SECRET_KEYS:
                raise ValueError(f"credential-shaped field is not allowed at {path}.{normalized}")
            _safe_payload(child, path=f"{path}.{normalized}", max_bytes=max_bytes)
    elif isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ValueError(f"adapter output list is too large at {path}")
        for index, child in enumerate(value):
            _safe_payload(child, path=f"{path}[{index}]", max_bytes=max_bytes)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite number is not allowed at {path}")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"non-JSON adapter value is not allowed at {path}")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")) > max_bytes:
        raise ValueError(f"adapter output exceeds {max_bytes} bytes")


def _aware_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("timestamp must be an aware datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _content_addressed_reference(value: str, *, namespace: str | None = None) -> bool:
    if not isinstance(value, str) or len(value) > 256 or not _SHA256_REF_RE.fullmatch(value):
        return False
    return namespace is None or value.startswith(f"{namespace}://")


def _blocked(
    task: dict[str, Any],
    *,
    started_at: datetime,
    code: str,
    message: str,
) -> SpecialistOutcome:
    result = _result(
        task,
        status="blocked",
        summary=message,
        artifact_refs=[],
        evidence_refs=[],
        findings=[{"severity": "high", "code": code, "message": message[:1000]}],
        started_at=started_at,
        source_commit=task["input"].get("commit_sha"),
        artifact_digest=task["input"].get("artifact_digest"),
    )
    return SpecialistOutcome(result=result)


@dataclass(frozen=True)
class RunbookAction:
    """Host-registered action; the task may select its ID but cannot define it."""

    action_id: str
    parameter_schema: Mapping[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        }
    )
    destructive: bool = False
    approval_required: bool = False
    approval_scope: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not _ID_RE.fullmatch(self.action_id):
            raise ValueError("runbook action ID is invalid")
        if type(self.destructive) is not bool or type(self.approval_required) is not bool:
            raise ValueError("runbook action approval flags must be booleans")
        if self.destructive and not self.approval_required:
            raise ValueError("destructive runbook actions must require human approval")
        if self.approval_required and self.approval_scope not in _APPROVAL_SCOPES:
            raise ValueError("approval-required runbook action needs a valid approval scope")
        if not self.approval_required and self.approval_scope is not None:
            raise ValueError("approval scope cannot be set for an action that needs no approval")
        schema = dict(self.parameter_schema)
        jsonschema.Draft202012Validator.check_schema(schema)
        object.__setattr__(self, "parameter_schema", MappingProxyType(schema))


@dataclass(frozen=True)
class RunbookDefinition:
    """Host-controlled immutable runbook binding and service allowlist."""

    runbook_id: str
    reference: str
    allowed_services: frozenset[str]
    actions: Mapping[str, RunbookAction]
    max_execution_seconds: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.runbook_id, str) or not _ID_RE.fullmatch(self.runbook_id):
            raise ValueError("runbook ID is invalid")
        if not _content_addressed_reference(self.reference, namespace="artifact"):
            raise ValueError("runbook reference must be an immutable artifact://sha256 reference")
        services = frozenset(self.allowed_services)
        if not services or any(not isinstance(s, str) or not _ID_RE.fullmatch(s) for s in services):
            raise ValueError("runbook must allow at least one valid service ID")
        actions = dict(self.actions)
        if not actions or any(
            key != action.action_id or not isinstance(action, RunbookAction)
            for key, action in actions.items()
        ):
            raise ValueError("runbook action registry is invalid")
        if isinstance(self.max_execution_seconds, bool) or not 1 <= self.max_execution_seconds <= 3600:
            raise ValueError("runbook max execution must be between 1 and 3600 seconds")
        object.__setattr__(self, "allowed_services", services)
        object.__setattr__(self, "actions", MappingProxyType(actions))


@dataclass(frozen=True)
class RunbookExecutionResult:
    """Structured host-runner receipt; it contains no command or credential data."""

    status: str
    summary: str
    started_at: datetime | str
    completed_at: datetime | str
    side_effect: str = "NONE"
    details: Mapping[str, Any] = field(default_factory=dict)
    findings: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed"}:
            raise ValueError("runbook execution status must be passed or failed")
        if self.side_effect not in {"NONE", "APPLIED", "UNKNOWN", "RECONCILED"}:
            raise ValueError("runbook side-effect status is invalid")
        if not isinstance(self.summary, str) or not self.summary.strip() or len(self.summary) > 8000:
            raise ValueError("runbook summary is invalid")
        _aware_timestamp(self.started_at)
        _aware_timestamp(self.completed_at)
        _safe_payload(self.details)
        if len(self.findings) > 64:
            raise ValueError("runbook returned too many findings")
        for finding in self.findings:
            if set(finding) - {"severity", "code", "message"}:
                raise ValueError("runbook finding contains unsupported fields")
            if finding.get("severity") not in {"info", "low", "medium", "high", "critical"}:
                raise ValueError("runbook finding severity is invalid")
            if not isinstance(finding.get("code"), str) or not _ID_RE.fullmatch(finding["code"]):
                raise ValueError("runbook finding code is invalid")
            if not isinstance(finding.get("message"), str) or not 1 <= len(finding["message"]) <= 1000:
                raise ValueError("runbook finding message is invalid")


class RunbookRunner(Protocol):
    """Privileged host adapter that maps registered IDs to prebuilt operations."""

    async def execute(
        self,
        *,
        runbook_id: str,
        action_id: str,
        service: str,
        parameters: Mapping[str, Any],
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> RunbookExecutionResult: ...


class OperatePolicyAuthorizer(Protocol):
    """Policy Engine adapter; false or an exception must fail closed."""

    def authorize_operation(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        task_id: str,
        service: str,
        runbook_id: str,
        action_id: str,
        risk: str,
    ) -> bool: ...


class OperateApprovalVerifier(Protocol):
    """Verifies a human approval bound to the exact tenant/task/action/resource."""

    def verify_approval(
        self,
        approval_ref: str,
        *,
        tenant_id: str,
        workflow_id: str,
        task_id: str,
        action: str,
        resource_ref: str,
    ) -> bool: ...


class OperateAgentExecutor:
    """Execute only host-registered runbook actions and persist an incident record."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runbooks: Mapping[str, RunbookDefinition],
        runner: RunbookRunner,
        policy_authorizer: OperatePolicyAuthorizer,
        approval_verifier: OperateApprovalVerifier | None = None,
    ) -> None:
        registry = dict(runbooks)
        if not registry or any(key != value.runbook_id for key, value in registry.items()):
            raise ValueError("Operate Agent requires a valid non-empty host runbook registry")
        if not callable(getattr(runner, "execute", None)):
            raise TypeError("Operate Agent requires a host runbook runner")
        if not callable(getattr(policy_authorizer, "authorize_operation", None)):
            raise TypeError("Operate Agent requires a fail-closed Policy Engine adapter")
        self.artifact_store = artifact_store
        self.runbooks = MappingProxyType(registry)
        self.runner = runner
        self.policy_authorizer = policy_authorizer
        self.approval_verifier = approval_verifier

    @staticmethod
    def _validate_task(task: dict[str, Any], principal: str) -> None:
        policy = authorize_agent_task(task, authenticated_principal=principal)
        if policy.agent_id != OPERATE_AGENT:
            raise OperationsPolicyError("Operate Agent accepts only rusa_operate_agent tasks")
        required = {
            "metrics.read", "logs.read", "runbook.read", "runbook.execute", "incident.write",
        }
        constraints = task["constraints"]
        if not required.issubset(set(constraints["allowed_tools"])):
            raise OperationsPolicyError("Operate task is missing a required operations capability")
        if constraints["network_access"] or constraints.get("allowed_network_destinations"):
            raise OperationsPolicyError("Operate Agent cannot receive task-controlled network access")
        if not {"incidents/", "reports/"}.issubset(set(constraints["write_scope"])):
            raise OperationsPolicyError("Operate task write scope must include incidents/ and reports/")

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None = None,
    ) -> SpecialistOutcome:
        if authenticated_principal != OPERATE_AGENT:
            raise ExecutionLeaseError("Operate Agent requires its authenticated workload identity")
        self._validate_task(task, authenticated_principal)
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        task_input = task["input"]
        service = task_input["service"]
        runbook_ref = task_input["runbook_ref"]
        if not _content_addressed_reference(runbook_ref, namespace="artifact"):
            return _blocked(
                task, started_at=started, code="operate.runbook_reference_invalid",
                message="Operate task must reference an immutable approved runbook artifact.",
            )
        try:
            invocation = self.artifact_store.get_json(task["tenant_id"], runbook_ref)
            if set(invocation) != {"runbook_id", "action_id", "parameters"}:
                raise ValueError("runbook invocation has unsupported fields")
            runbook_id = invocation["runbook_id"]
            action_id = invocation["action_id"]
            parameters = invocation["parameters"]
            definition = self.runbooks.get(runbook_id)
            if definition is None or definition.reference != runbook_ref:
                raise ValueError("runbook is not registered for this immutable artifact")
            if service not in definition.allowed_services:
                raise ValueError("runbook is not allowlisted for the requested service")
            action = definition.actions.get(action_id)
            if action is None or not isinstance(parameters, dict):
                raise ValueError("runbook action or parameters are not registered")
            jsonschema.Draft202012Validator(action.parameter_schema).validate(parameters)
            _safe_payload(parameters)
        except Exception:
            return _blocked(
                task, started_at=started, code="operate.runbook_validation_failed",
                message="Runbook reference, service, action, or parameters failed host policy validation.",
            )

        risk = "high" if action.destructive or action.approval_required else "low"
        try:
            authorized = self.policy_authorizer.authorize_operation(
                tenant_id=task["tenant_id"],
                workflow_id=task["workflow_id"],
                task_id=task["task_id"],
                service=service,
                runbook_id=runbook_id,
                action_id=action_id,
                risk=risk,
            )
        except Exception:
            authorized = False
        if authorized is not True:
            return _blocked(
                task, started_at=started, code="operate.policy_denied",
                message="Policy Engine did not authorize this runbook action.",
            )

        constraints = task["constraints"]
        approval_required = constraints["approval_required"]
        approval_scope = set(constraints.get("approval_scope", []))
        if action.approval_required and (
            not approval_required or action.approval_scope not in approval_scope
        ):
            return _blocked(
                task, started_at=started, code="operate.human_approval_required",
                message="The selected runbook action requires its configured human approval scope.",
            )
        if approval_required:
            approval_ref = constraints.get("approval_ref")
            if not isinstance(approval_ref, str) or not approval_ref or self.approval_verifier is None:
                return _blocked(
                    task, started_at=started, code="operate.approval_unavailable",
                    message="A verifiable human approval reference is required before runbook execution.",
                )
            try:
                approved = self.approval_verifier.verify_approval(
                    approval_ref,
                    tenant_id=task["tenant_id"],
                    workflow_id=task["workflow_id"],
                    task_id=task["task_id"],
                    action=action_id,
                    resource_ref=service,
                )
            except Exception:
                approved = False
            if approved is not True:
                return _blocked(
                    task, started_at=started, code="operate.approval_not_verified",
                    message="The human approval is pending, rejected, expired, or bound to another action.",
                )
        elif constraints.get("approval_scope") or constraints.get("approval_ref"):
            return _blocked(
                task, started_at=started, code="operate.approval_scope_invalid",
                message="Operate task supplied approval context without requiring approval.",
            )

        _checkpoint(checkpoint)
        timeout = min(_timeout_seconds(task), float(definition.max_execution_seconds))
        side_effect = "UNKNOWN"
        try:
            execution = await asyncio.wait_for(
                self.runner.execute(
                    runbook_id=runbook_id,
                    action_id=action_id,
                    service=service,
                    parameters=MappingProxyType(dict(parameters)),
                    timeout_seconds=timeout,
                    checkpoint=checkpoint,
                ),
                timeout=timeout,
            )
            if not isinstance(execution, RunbookExecutionResult):
                raise TypeError("runbook runner did not return a typed execution receipt")
            side_effect = execution.side_effect
            try:
                _checkpoint(checkpoint)
            except ExecutionLeaseError:
                return _failure(
                    task, started_at=started, code="operate.lease_lost_after_action",
                    message="Runbook execution returned after its lease was lost; side-effect reconciliation is required.",
                    failure_category="AMBIGUOUS", side_effect=side_effect,
                )
            report = {
                "schema_version": 1,
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "agent_id": OPERATE_AGENT,
                "service": service,
                "runbook_id": runbook_id,
                "runbook_ref": runbook_ref,
                "action_id": action_id,
                "incident_ref": task_input.get("incident_ref"),
                "status": execution.status,
                "summary": execution.summary,
                "side_effect": execution.side_effect,
                "started_at": _aware_timestamp(execution.started_at),
                "completed_at": _aware_timestamp(execution.completed_at),
                "details": dict(execution.details),
                "findings": [dict(row) for row in execution.findings],
            }
            report_ref = self.artifact_store.put_json(
                task["tenant_id"], report, namespace="artifact"
            )
            evidence_ref = self.artifact_store.put_json(
                task["tenant_id"],
                {
                    "schema_version": 1,
                    "tenant_id": task["tenant_id"],
                    "workflow_id": task["workflow_id"],
                    "task_id": task["task_id"],
                    "agent_id": OPERATE_AGENT,
                    "artifact_ref": report_ref,
                    "runbook_ref": runbook_ref,
                    "action_id": action_id,
                    "side_effect": execution.side_effect,
                    "created_at": _timestamp(),
                },
                namespace="evidence",
            )
            result = _result(
                task,
                status=execution.status,
                summary=execution.summary,
                artifact_refs=[report_ref],
                evidence_refs=[evidence_ref],
                findings=[
                    {
                        "severity": row["severity"],
                        "code": row["code"],
                        "message": row["message"],
                    }
                    for row in execution.findings
                ],
                started_at=started,
                handoff=(
                    {"next_agent": MONITOR_AGENT, "event": "operate.recovered"}
                    if execution.status == "passed"
                    else None
                ),
                source_commit=task_input.get("commit_sha"),
                artifact_digest=task_input.get("artifact_digest"),
            )
            return SpecialistOutcome(
                result=result,
                failure_category=(
                    "PERMANENT" if execution.status == "failed" and side_effect == "NONE"
                    else "AMBIGUOUS" if execution.status == "failed"
                    else ""
                ),
                side_effect=side_effect,
            )
        except asyncio.TimeoutError:
            return _failure(
                task, started_at=started, code="operate.runbook_timeout",
                message="Runbook execution exceeded its bounded time budget; reconciliation is required.",
                failure_category="AMBIGUOUS", side_effect="UNKNOWN",
            )
        except Exception:
            return _failure(
                task, started_at=started, code="operate.runbook_execution_ambiguous",
                message="Runbook execution or incident evidence persistence did not return a confirmed result.",
                failure_category="AMBIGUOUS", side_effect=side_effect,
            )


@dataclass(frozen=True)
class SLORule:
    """Host-owned deterministic SLO threshold; task input cannot weaken it."""

    rule_id: str
    metric_name: str
    operator: str
    threshold: float
    severity: str
    description: str
    recommendation: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.rule_id) or not _ID_RE.fullmatch(self.metric_name):
            raise ValueError("SLO rule or metric ID is invalid")
        if self.operator not in {"lte", "gte"}:
            raise ValueError("SLO operator must be lte or gte")
        if type(self.threshold) not in {int, float} or not math.isfinite(float(self.threshold)):
            raise ValueError("SLO threshold must be finite")
        if self.severity not in {"info", "low", "medium", "high", "critical"}:
            raise ValueError("SLO severity is invalid")
        if not 1 <= len(self.description) <= 1000 or not 1 <= len(self.recommendation) <= 1000:
            raise ValueError("SLO description or recommendation is invalid")


@dataclass(frozen=True)
class TelemetryFeedback:
    """Pre-aggregated recurring trend from a trusted telemetry adapter."""

    code: str
    severity: str
    message: str
    recommendation: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.code):
            raise ValueError("telemetry feedback code is invalid")
        if self.severity not in {"info", "low", "medium", "high", "critical"}:
            raise ValueError("telemetry feedback severity is invalid")
        if not 1 <= len(self.message) <= 1000 or not 1 <= len(self.recommendation) <= 1000:
            raise ValueError("telemetry feedback text is invalid")


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Bounded read-only telemetry result returned by the host adapter."""

    observed_at: datetime | str
    metrics: Mapping[str, int | float]
    log_count: int
    trace_count: int
    evidence_refs: tuple[str, ...] = ()
    feedback: tuple[TelemetryFeedback, ...] = ()

    def __post_init__(self) -> None:
        _aware_timestamp(self.observed_at)
        if len(self.metrics) > 256:
            raise ValueError("telemetry snapshot contains too many metrics")
        for name, value in self.metrics.items():
            if not isinstance(name, str) or not _ID_RE.fullmatch(name):
                raise ValueError("telemetry metric name is invalid")
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise ValueError("telemetry metric values must be finite numbers")
        if (
            isinstance(self.log_count, bool) or not isinstance(self.log_count, int) or self.log_count < 0
            or isinstance(self.trace_count, bool) or not isinstance(self.trace_count, int) or self.trace_count < 0
        ):
            raise ValueError("telemetry log/trace counts must be non-negative integers")
        if len(self.evidence_refs) > 100 or any(
            not _content_addressed_reference(ref) for ref in self.evidence_refs
        ):
            raise ValueError("telemetry evidence references must be immutable content-addressed URIs")
        if len(self.feedback) > 64 or any(not isinstance(row, TelemetryFeedback) for row in self.feedback):
            raise ValueError("telemetry feedback list is invalid")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


class TelemetryReader(Protocol):
    """Read-only host adapter for metrics, logs, traces, and alert projections."""

    async def collect(
        self,
        *,
        service: str,
        environment: str,
        window: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> TelemetrySnapshot: ...


class MonitorAgentExecutor:
    """Evaluate host-defined SLOs from read-only, evidence-backed telemetry."""

    def __init__(
        self,
        *,
        telemetry_reader: TelemetryReader,
        rules_by_service: Mapping[str, tuple[SLORule, ...] | list[SLORule]],
        allowed_windows: frozenset[str] = frozenset({"5m", "15m", "1h", "6h", "24h"}),
        allowed_environments: frozenset[str] = frozenset({"development", "eval", "staging", "production"}),
    ) -> None:
        rules = {service: tuple(rows) for service, rows in rules_by_service.items()}
        if not rules or any(
            not _ID_RE.fullmatch(service) or not rows
            or any(not isinstance(row, SLORule) for row in rows)
            or len({row.rule_id for row in rows}) != len(rows)
            for service, rows in rules.items()
        ):
            raise ValueError("Monitor Agent requires valid host-owned SLO rules per service")
        if not allowed_windows or any(window not in {"5m", "15m", "1h", "6h", "24h"} for window in allowed_windows):
            raise ValueError("Monitor Agent window allowlist is invalid")
        if not allowed_environments or not allowed_environments.issubset(
            {"development", "eval", "staging", "production"}
        ):
            raise ValueError("Monitor Agent environment allowlist is invalid")
        if not callable(getattr(telemetry_reader, "collect", None)):
            raise TypeError("Monitor Agent requires a read-only telemetry adapter")
        self.telemetry_reader = telemetry_reader
        self.rules_by_service = MappingProxyType(rules)
        self.allowed_windows = frozenset(allowed_windows)
        self.allowed_environments = frozenset(allowed_environments)

    @staticmethod
    def _validate_task(task: dict[str, Any], principal: str) -> None:
        policy = authorize_agent_task(task, authenticated_principal=principal)
        if policy.agent_id != MONITOR_AGENT or policy.read_only is not True:
            raise OperationsPolicyError("Monitor Agent requires its registered read-only identity")
        required = {"metrics.read", "logs.read", "traces.read", "alerts.read"}
        constraints = task["constraints"]
        if not required.issubset(set(constraints["allowed_tools"])):
            raise OperationsPolicyError("Monitor task is missing a required telemetry read capability")
        if (
            constraints["network_access"]
            or constraints.get("allowed_network_destinations")
            or constraints["write_scope"]
            or constraints["approval_required"]
            or constraints.get("approval_scope")
            or constraints.get("approval_ref")
        ):
            raise OperationsPolicyError("Monitor Agent is read-only and cannot receive write, network, or approval authority")

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None = None,
    ) -> SpecialistOutcome:
        if authenticated_principal != MONITOR_AGENT:
            raise ExecutionLeaseError("Monitor Agent requires its authenticated workload identity")
        self._validate_task(task, authenticated_principal)
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        task_input = task["input"]
        service = task_input["service"]
        environment = task_input.get("environment", "staging")
        window = task_input["metrics_window"]
        rules = self.rules_by_service.get(service)
        if rules is None or environment not in self.allowed_environments or window not in self.allowed_windows:
            return _blocked(
                task, started_at=started, code="monitor.scope_not_allowlisted",
                message="Monitor service, environment, or telemetry window is outside host policy.",
            )
        timeout = _timeout_seconds(task)
        try:
            snapshot = await asyncio.wait_for(
                self.telemetry_reader.collect(
                    service=service,
                    environment=environment,
                    window=window,
                    timeout_seconds=timeout,
                    checkpoint=checkpoint,
                ),
                timeout=timeout,
            )
            if not isinstance(snapshot, TelemetrySnapshot):
                raise TypeError("telemetry adapter did not return a typed snapshot")
            _checkpoint(checkpoint)
            evidence_refs = list(dict.fromkeys(
                list(task_input.get("evidence_refs", [])) + list(snapshot.evidence_refs)
            ))
            if not evidence_refs:
                return _blocked(
                    task, started_at=started, code="monitor.telemetry_evidence_missing",
                    message="Telemetry snapshot has no immutable evidence reference and cannot support an auditable finding.",
                )
            findings: list[dict[str, str]] = []
            alert_count = 0
            for rule in rules:
                actual = snapshot.metrics.get(rule.metric_name)
                if actual is None:
                    message = f"{rule.description} Required metric {rule.metric_name} was not observed."
                    severity = "medium"
                    code = "monitor.metric_missing." + rule.rule_id
                else:
                    violated = actual > rule.threshold if rule.operator == "lte" else actual < rule.threshold
                    if not violated:
                        continue
                    message = (
                        f"{rule.description} Observed {rule.metric_name}={actual}; "
                        f"host threshold is {rule.operator} {rule.threshold}. "
                        f"Recommendation: {rule.recommendation}"
                    )
                    severity = rule.severity
                    fingerprint = hashlib.sha256(
                        f"{task['tenant_id']}|{service}|{rule.rule_id}|{window}".encode("utf-8")
                    ).hexdigest()[:24]
                    code = f"monitor.slo_breach.{fingerprint}"
                alert_count += 1
                findings.append(
                    {
                        "severity": severity,
                        "code": code,
                        "message": message[:1000],
                        "evidence_ref": evidence_refs[0],
                    }
                )
            for feedback in snapshot.feedback:
                findings.append(
                    {
                        "severity": feedback.severity,
                        "code": f"monitor.feedback.{feedback.code}",
                        "message": f"{feedback.message} Recommendation: {feedback.recommendation}"[:1000],
                        "evidence_ref": evidence_refs[0],
                    }
                )
            handoff = None
            if alert_count:
                handoff = {"next_agent": OPERATE_AGENT, "event": "monitor.alert_triggered"}
            elif snapshot.feedback:
                handoff = {"next_agent": PLAN_AGENT, "event": "monitor.feedback_created"}
            summary = (
                f"Read-only monitoring completed for {service} in {environment} over {window}. "
                f"Observed at {_aware_timestamp(snapshot.observed_at)}; "
                f"{len(snapshot.metrics)} metrics, {snapshot.log_count} log records, "
                f"{snapshot.trace_count} traces, {alert_count} SLO alerts, "
                f"{len(snapshot.feedback)} recurring feedback findings."
            )
            result = _result(
                task,
                status="passed",
                summary=summary,
                artifact_refs=[],
                evidence_refs=evidence_refs,
                findings=findings,
                started_at=started,
                handoff=handoff,
                source_commit=task_input.get("commit_sha"),
                artifact_digest=task_input.get("artifact_digest"),
            )
            return SpecialistOutcome(result=result)
        except asyncio.TimeoutError:
            return _failure(
                task, started_at=started, code="monitor.telemetry_timeout",
                message="Read-only telemetry collection exceeded its bounded time budget.",
                failure_category="TRANSIENT",
            )
        except Exception:
            return _failure(
                task, started_at=started, code="monitor.telemetry_invalid",
                message="Telemetry collection failed validation or did not return a trusted snapshot.",
                failure_category="TRANSIENT",
            )
