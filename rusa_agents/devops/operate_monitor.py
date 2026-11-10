"""Operate, Monitor, and cross-cutting Security Guard specialist executors.

All service access is delegated to host-injected adapters. Task payloads carry
only named runbook actions, content-addressed references, and bounded SLO data;
they never carry shell commands, endpoints, or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

import jsonschema

from rusa_agents.policy import AutonomyPolicy, RiskLevel

from .agent_registry import (
    DEPLOY_AGENT,
    MONITOR_AGENT,
    MONITOR_READ_ONLY_TOOLS,
    OPERATE_AGENT,
    READ_ONLY_TOOLS,
    SECURITY_GUARD_AGENT,
    authorize_agent_task,
)
from .artifacts import ArtifactStoreError, ContentAddressedArtifactStore
from .build_test import (
    EvidenceSigner,
    EvidenceVerifier,
    _load_json_artifact,
    _verified_evidence,
    _validate_time_window,
)
from .deploy import DeploymentTarget, _verify_approval
from .release import ApprovalVerifier
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

_ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_METRICS_WINDOWS = frozenset(
    {"5m", "15m", "1h", "6h", "24h", "last-5m", "last-15m", "last-1h", "last-6h", "last-24h"}
)
_DEPLOY_EVIDENCE_TYPE = "rusa.devops.deploy.attestation"

_RUNBOOK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "service", "actions"],
    "properties": {
        "schema_version": {"const": 1},
        "service": {"type": "string", "minLength": 1, "maxLength": 128},
        "actions": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": _ACTION_ID_RE.pattern},
        },
    },
}

_SLO_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "availability_target",
        "max_p95_latency_ms",
        "max_error_rate",
        "max_queue_depth",
        "max_cpu_percent",
        "max_memory_percent",
        "max_disk_percent",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "availability_target": {"type": "number", "minimum": 0, "maximum": 1},
        "max_p95_latency_ms": {"type": "number", "exclusiveMinimum": 0},
        "max_error_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "max_queue_depth": {"type": "integer", "minimum": 0},
        "max_cpu_percent": {"type": "number", "minimum": 0, "maximum": 100},
        "max_memory_percent": {"type": "number", "minimum": 0, "maximum": 100},
        "max_disk_percent": {"type": "number", "minimum": 0, "maximum": 100},
    },
}


def _verified_deployment_context(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    workflow_id: str,
    service: str,
    evidence_ref: str,
    verifier: EvidenceVerifier,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = _verified_evidence(
        store,
        tenant_id,
        evidence_ref,
        verifier=verifier,
        expected_producer=DEPLOY_AGENT,
        expected_type=_DEPLOY_EVIDENCE_TYPE,
    )
    if any(
        evidence.get(key) != expected
        for key, expected in (
            ("tenant_id", tenant_id),
            ("workflow_id", workflow_id),
            ("service", service),
            ("environment", "staging"),
            ("status", "passed"),
            ("agent_id", DEPLOY_AGENT),
        )
    ):
        raise ValueError("Deploy evidence does not match the tenant, workflow, service, or staging environment")
    record_ref = evidence.get("deployment_record_ref")
    record_digest = evidence.get("deployment_record_digest")
    if not isinstance(record_ref, str) or not isinstance(record_digest, str):
        raise ValueError("Deploy evidence is missing its deployment record reference or digest")
    raw = store.get(tenant_id, record_ref)
    if hashlib.sha256(raw).hexdigest() != record_digest:
        raise ValueError("deployment record bytes do not match the signed Deploy digest")
    record = _load_json_artifact(store, tenant_id, record_ref, label="deployment record")
    if any(
        record.get(key) != expected
        for key, expected in (
            ("tenant_id", tenant_id),
            ("workflow_id", workflow_id),
            ("service", service),
            ("environment", "staging"),
            ("status", "passed"),
            ("target_id", evidence.get("target_id")),
            ("deployment_id", evidence.get("deployment_id")),
            ("source_commit", evidence.get("source_commit")),
            ("artifact_digest", evidence.get("artifact_digest")),
        )
    ):
        raise ValueError("deployment record does not match its signed Deploy attestation")
    if (
        not isinstance(evidence.get("target_id"), str)
        or not isinstance(evidence.get("deployment_id"), str)
        or not isinstance(evidence.get("source_commit"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", evidence["source_commit"])
        or not isinstance(evidence.get("artifact_digest"), str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", evidence["artifact_digest"])
    ):
        raise ValueError("Deploy evidence identity fields are invalid")
    return evidence, record


def _validate_target_map(
    targets: Mapping[str, DeploymentTarget],
    *,
    label: str,
) -> Mapping[str, DeploymentTarget]:
    values = dict(targets)
    if not values:
        raise ValueError(f"{label} requires at least one host-configured staging target")
    for target_id, target in values.items():
        if (
            not isinstance(target, DeploymentTarget)
            or target_id != target.target_id
            or target.environment != "staging"
            or not target.allowed_services
        ):
            raise ValueError(f"{label} contains an invalid or non-staging target")
    return MappingProxyType(values)


def _validate_task_network(task: dict[str, Any]) -> None:
    constraints = task["constraints"]
    if constraints["network_access"] or constraints.get("allowed_network_destinations"):
        raise ValueError("specialist tasks cannot receive task-controlled network access")


@dataclass(frozen=True)
class OperateActionPolicy:
    policy_action: str
    risk: RiskLevel
    requires_approval: bool = False


@dataclass(frozen=True)
class OperatePolicy:
    allowed_targets: Mapping[str, DeploymentTarget]
    allowed_actions: Mapping[str, OperateActionPolicy]
    require_approval_for_mutations: bool = True

    def __post_init__(self) -> None:
        targets = _validate_target_map(self.allowed_targets, label="Operate policy")
        actions = dict(self.allowed_actions)
        if type(self.require_approval_for_mutations) is not bool:
            raise ValueError("require_approval_for_mutations must be a boolean")
        for action_id, policy in actions.items():
            if (
                not _ACTION_ID_RE.fullmatch(action_id)
                or not isinstance(policy, OperateActionPolicy)
                or not isinstance(policy.risk, RiskLevel)
                or not _CODE_RE.fullmatch(policy.policy_action)
                or type(policy.requires_approval) is not bool
            ):
                raise ValueError("Operate policy contains an invalid action policy")
        object.__setattr__(self, "allowed_targets", targets)
        object.__setattr__(self, "allowed_actions", MappingProxyType(actions))


@dataclass(frozen=True)
class DependencyStatus:
    name: str
    status: str


@dataclass(frozen=True)
class OperationalSnapshot:
    target_id: str
    service: str
    deployment_id: str
    observed_digest: str
    status: str
    dependencies: tuple[DependencyStatus, ...]
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    queue_depth: int
    sampled_at: datetime | str


@dataclass(frozen=True)
class RunbookActionResult:
    action_id: str
    status: str
    deployment_id: str
    started_at: datetime | str
    completed_at: datetime | str


class OperateRunner(Protocol):
    async def inspect_service(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        deployment_id: str,
        expected_digest: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> OperationalSnapshot: ...

    async def execute_action(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        deployment_id: str,
        expected_digest: str,
        action_id: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> RunbookActionResult: ...


class OperationalRecordWriter(Protocol):
    async def write_operational_record(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        task_id: str,
        record: dict[str, Any],
        incident: bool,
        timeout_seconds: float,
    ) -> str: ...


def _validate_operational_snapshot(
    snapshot: OperationalSnapshot,
    *,
    target: DeploymentTarget,
    service: str,
    deployment_id: str,
    artifact_digest: str,
) -> dict[str, Any]:
    if (
        not isinstance(snapshot, OperationalSnapshot)
        or snapshot.target_id != target.target_id
        or snapshot.service != service
        or snapshot.deployment_id != deployment_id
        or snapshot.observed_digest != artifact_digest
        or snapshot.status not in {"healthy", "degraded", "unhealthy"}
    ):
        raise ValueError("Operate runner did not inspect the exact deployed staging service")
    sampled_at = _timestamp(datetime.fromisoformat(
        snapshot.sampled_at.replace("Z", "+00:00")
        if isinstance(snapshot.sampled_at, str)
        else snapshot.sampled_at.isoformat()
    ))
    metrics = {
        "cpu_percent": snapshot.cpu_percent,
        "memory_percent": snapshot.memory_percent,
        "disk_percent": snapshot.disk_percent,
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value > 100
        for value in metrics.values()
    ):
        raise ValueError("Operate runner returned invalid capacity metrics")
    if (
        isinstance(snapshot.queue_depth, bool)
        or not isinstance(snapshot.queue_depth, int)
        or snapshot.queue_depth < 0
    ):
        raise ValueError("Operate runner returned an invalid queue depth")
    dependencies: dict[str, str] = {}
    for item in snapshot.dependencies:
        if (
            not isinstance(item, DependencyStatus)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item.name)
            or item.status not in {"healthy", "degraded", "unhealthy"}
            or item.name in dependencies
        ):
            raise ValueError("Operate runner returned an invalid dependency status")
        dependencies[item.name] = item.status
    return {
        "target_id": snapshot.target_id,
        "service": snapshot.service,
        "deployment_id": snapshot.deployment_id,
        "observed_digest": snapshot.observed_digest,
        "status": snapshot.status,
        "dependencies": dependencies,
        **metrics,
        "queue_depth": snapshot.queue_depth,
        "sampled_at": sampled_at,
    }


def _validate_operate_task(task: dict[str, Any], principal: str) -> None:
    policy = authorize_agent_task(task, authenticated_principal=principal)
    if policy.stage != "operate":
        raise ValueError("Operate Agent is only authorized for the Operate stage")
    constraints = task["constraints"]
    # Artifact and deployment evidence reads are performed by the injected
    # host-side verifier; the agent's tool authority remains the registry ceiling.
    required = {
        "metrics.read", "logs.read", "runbook.read", "runbook.execute", "incident.write",
    }
    if not required.issubset(set(constraints["allowed_tools"])):
        raise ValueError("Operate task is missing a required health, runbook, or incident capability")
    _validate_task_network(task)
    if not {"incidents/", "reports/"}.issubset(set(constraints["write_scope"])):
        raise ValueError("Operate task write scope must include incidents/ and reports/")
    if constraints["approval_required"]:
        if set(constraints.get("approval_scope", [])) != {"operate"}:
            raise ValueError("Operate approval must be scoped only to operate")
    elif constraints.get("approval_scope") or constraints.get("approval_ref"):
        raise ValueError("Operate approval scope/reference cannot be supplied when approval is not required")


class OperateAgentExecutor:
    """Inspect a deployed service and execute only host-allowlisted runbook actions."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runner: OperateRunner,
        record_writer: OperationalRecordWriter,
        verifier: EvidenceVerifier,
        policy: OperatePolicy,
        autonomy_policy: AutonomyPolicy,
        approval_verifier: ApprovalVerifier | None = None,
    ):
        if not callable(getattr(runner, "inspect_service", None)) or not callable(
            getattr(runner, "execute_action", None)
        ):
            raise TypeError("Operate Agent runner is missing an inspection or action adapter")
        if not callable(getattr(record_writer, "write_operational_record", None)):
            raise TypeError("Operate Agent requires a scoped operational record writer")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("Operate Agent requires a trusted deployment evidence verifier")
        self.artifact_store = artifact_store
        self.runner = runner
        self.record_writer = record_writer
        self.verifier = verifier
        self.policy = policy
        self.autonomy_policy = autonomy_policy
        self.approval_verifier = approval_verifier

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None,
    ) -> SpecialistOutcome:
        if authenticated_principal != OPERATE_AGENT:
            raise ExecutionLeaseError("Operate Agent requires its authenticated workload identity")
        _validate_operate_task(task, authenticated_principal)
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        service = task["input"]["service"]
        action_id = task["input"].get("action_id")
        approval_ref = task["constraints"].get("approval_ref")
        try:
            deployment, _ = _verified_deployment_context(
                self.artifact_store,
                task["tenant_id"],
                task["workflow_id"],
                service,
                task["input"]["deployment_evidence_ref"],
                self.verifier,
            )
            runbook = _load_json_artifact(
                self.artifact_store,
                task["tenant_id"],
                task["input"]["runbook_ref"],
                label="runbook",
            )
            jsonschema.validate(runbook, _RUNBOOK_SCHEMA)
            if runbook["service"] != service:
                raise ValueError("runbook service does not match the task")
            if len(runbook["actions"]) != len(set(runbook["actions"])):
                raise ValueError("runbook contains duplicate action IDs")
        except (ArtifactStoreError, ValueError, jsonschema.ValidationError):
            return _failure(
                task,
                started_at=started,
                code="operate.inputs_invalid",
                message="Operate Agent could not verify the signed deployment evidence or bounded runbook.",
                status="blocked",
                failure_category="",
                source_commit=task["input"].get("commit_sha"),
                artifact_digest=task["input"].get("artifact_digest"),
            )

        target = self.policy.allowed_targets.get(deployment["target_id"])
        if target is None or service not in target.allowed_services:
            return _failure(
                task,
                started_at=started,
                code="operate.target_not_allowlisted",
                message="The deployed target or service is not in the host-controlled Operate allowlist.",
                status="blocked",
                failure_category="",
                source_commit=deployment["source_commit"],
                artifact_digest=deployment["artifact_digest"],
            )

        action_policy: OperateActionPolicy | None = None
        approval_id: str | None = None
        if action_id is not None:
            action_policy = self.policy.allowed_actions.get(action_id)
            if action_policy is None or action_id not in runbook["actions"]:
                return _failure(
                    task,
                    started_at=started,
                    code="operate.action_not_allowlisted",
                    message="The requested runbook action is not present in both the runbook and host allowlist.",
                    status="blocked",
                    failure_category="",
                    source_commit=deployment["source_commit"],
                    artifact_digest=deployment["artifact_digest"],
                )
            decision = self.autonomy_policy.evaluate(
                action_policy.policy_action,
                action_policy.risk,
            )
            if not decision.allowed and not decision.requires_approval:
                return _failure(
                    task,
                    started_at=started,
                    code="operate.policy_denied",
                    message="The deterministic autonomy policy denied the requested runbook action.",
                    status="blocked",
                    failure_category="",
                    source_commit=deployment["source_commit"],
                    artifact_digest=deployment["artifact_digest"],
                )
            approval_required = (
                self.policy.require_approval_for_mutations
                or action_policy.requires_approval
                or decision.requires_approval
                or task["constraints"]["approval_required"]
            )
            if approval_required and not approval_ref:
                result = _result(
                    task,
                    status="needs_approval",
                    summary="The requested operational action is waiting for human approval.",
                    artifact_refs=[],
                    evidence_refs=[],
                    findings=[{
                        "severity": "info",
                        "code": "operate.approval_required",
                        "message": "A human approval bound to the deployed commit and digest is required.",
                    }],
                    started_at=started,
                    approval_ref=f"approval://pending/operate/{task['task_id']}",
                    source_commit=deployment["source_commit"],
                    artifact_digest=deployment["artifact_digest"],
                )
                return SpecialistOutcome(result=result)
            if approval_required:
                if self.approval_verifier is None:
                    return _failure(
                        task,
                        started_at=started,
                        code="operate.approval_verifier_unavailable",
                        message="The host approval verifier is unavailable for this operational action.",
                        status="blocked",
                        failure_category="",
                        source_commit=deployment["source_commit"],
                        artifact_digest=deployment["artifact_digest"],
                    )
                try:
                    approval_id = _verify_approval(
                        self.approval_verifier,
                        approval_ref,
                        stage="operate",
                        tenant_id=task["tenant_id"],
                        workflow_id=task["workflow_id"],
                        source_commit=deployment["source_commit"],
                        artifact_digest=deployment["artifact_digest"],
                    )
                except Exception:
                    return _failure(
                        task,
                        started_at=started,
                        code="operate.approval_invalid",
                        message="Operational approval is invalid or is not bound to the deployed commit and digest.",
                        status="blocked",
                        failure_category="",
                        source_commit=deployment["source_commit"],
                        artifact_digest=deployment["artifact_digest"],
                    )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _timeout_seconds(task)

        def remaining() -> float:
            _checkpoint(checkpoint)
            value = deadline - loop.time()
            if value <= 0:
                raise asyncio.TimeoutError
            return value

        action_started = False
        try:
            before = await asyncio.wait_for(
                self.runner.inspect_service(
                    target=target,
                    service=service,
                    deployment_id=deployment["deployment_id"],
                    expected_digest=deployment["artifact_digest"],
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            before_record = _validate_operational_snapshot(
                before,
                target=target,
                service=service,
                deployment_id=deployment["deployment_id"],
                artifact_digest=deployment["artifact_digest"],
            )
            action_record: dict[str, Any] | None = None
            if action_id is not None:
                action_started = True
                action = await asyncio.wait_for(
                    self.runner.execute_action(
                        target=target,
                        service=service,
                        deployment_id=deployment["deployment_id"],
                        expected_digest=deployment["artifact_digest"],
                        action_id=action_id,
                        timeout_seconds=remaining(),
                        checkpoint=checkpoint,
                    ),
                    timeout=remaining(),
                )
                if (
                    not isinstance(action, RunbookActionResult)
                    or action.action_id != action_id
                    or action.deployment_id != deployment["deployment_id"]
                    or action.status not in {"passed", "failed"}
                ):
                    raise ValueError("runbook adapter returned an invalid action result")
                action_started_at, action_completed_at = _validate_time_window(
                    action.started_at, action.completed_at
                )
                action_record = {
                    "action_id": action.action_id,
                    "status": action.status,
                    "started_at": action_started_at,
                    "completed_at": action_completed_at,
                }
                after = await asyncio.wait_for(
                    self.runner.inspect_service(
                        target=target,
                        service=service,
                        deployment_id=deployment["deployment_id"],
                        expected_digest=deployment["artifact_digest"],
                        timeout_seconds=remaining(),
                        checkpoint=checkpoint,
                    ),
                    timeout=remaining(),
                )
            else:
                after = before
            after_record = _validate_operational_snapshot(
                after,
                target=target,
                service=service,
                deployment_id=deployment["deployment_id"],
                artifact_digest=deployment["artifact_digest"],
            )
            findings: list[dict[str, str]] = []
            if after.status != "healthy":
                findings.append({
                    "severity": "high" if after.status == "unhealthy" else "medium",
                    "code": "operate.service_not_healthy",
                    "message": "The staging service did not reach a healthy state after operational inspection or recovery.",
                    "evidence_ref": task["input"]["deployment_evidence_ref"],
                })
            for name, status in after_record["dependencies"].items():
                if status != "healthy":
                    findings.append({
                        "severity": "high" if status == "unhealthy" else "medium",
                        "code": "operate.dependency_not_healthy",
                        "message": f"Dependency {name} is {status}.",
                        "evidence_ref": task["input"]["deployment_evidence_ref"],
                    })
            if action_record and action_record["status"] != "passed":
                findings.append({
                    "severity": "high",
                    "code": "operate.runbook_action_failed",
                    "message": "The allowlisted runbook action did not complete successfully.",
                    "evidence_ref": task["input"]["runbook_ref"],
                })
            status = "passed" if not findings else "failed"
            record = {
                "schema_version": 1,
                "type": "rusa.devops.operational.record",
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "service": service,
                "target_id": target.target_id,
                "deployment_evidence_ref": task["input"]["deployment_evidence_ref"],
                "deployment_id": deployment["deployment_id"],
                "source_commit": deployment["source_commit"],
                "artifact_digest": deployment["artifact_digest"],
                "runbook_ref": task["input"]["runbook_ref"],
                "action": action_record,
                "before": before_record,
                "after": after_record,
                "approval_id": approval_id,
                "status": status,
                "created_at": _timestamp(),
                "policy_version": task["audit"]["policy_version"],
            }
            record_ref = await asyncio.wait_for(
                self.record_writer.write_operational_record(
                    tenant_id=task["tenant_id"],
                    workflow_id=task["workflow_id"],
                    task_id=task["task_id"],
                    record=record,
                    incident=bool(findings),
                    timeout_seconds=remaining(),
                ),
                timeout=remaining(),
            )
            parsed_ref = urlsplit(record_ref) if isinstance(record_ref, str) else None
            if parsed_ref is None or parsed_ref.scheme not in {"report", "incident"} or not parsed_ref.netloc:
                raise ValueError("operational record writer returned an invalid record reference")
            result = _result(
                task,
                status=status,
                summary=(
                    "Operational checks passed; the service and dependencies are healthy."
                    if status == "passed"
                    else "Operational checks identified an unresolved service or dependency issue."
                ),
                artifact_refs=[record_ref],
                evidence_refs=[],
                findings=findings,
                started_at=started,
                handoff=(
                    {"next_agent": MONITOR_AGENT, "event": "operate.recovered"}
                    if status == "passed"
                    else None
                ),
                source_commit=deployment["source_commit"],
                artifact_digest=deployment["artifact_digest"],
                approval_ref=approval_ref,
            )
            return SpecialistOutcome(
                result=result,
                failure_category="" if status == "passed" else "PERMANENT",
                side_effect="APPLIED" if action_started else "NONE",
            )
        except ExecutionLeaseError:
            raise
        except asyncio.TimeoutError:
            return _failure(
                task,
                started_at=started,
                code="operate.execution_timeout",
                message="Operational inspection or recovery exceeded its execution deadline.",
                failure_category="AMBIGUOUS" if action_started else "TRANSIENT",
                side_effect="UNKNOWN" if action_started else "NONE",
                source_commit=deployment["source_commit"],
                artifact_digest=deployment["artifact_digest"],
            )
        except Exception:
            return _failure(
                task,
                started_at=started,
                code="operate.execution_failed",
                message="Operate Agent could not verify the operational result or persist its report.",
                failure_category="AMBIGUOUS" if action_started else "PERMANENT",
                side_effect="UNKNOWN" if action_started else "NONE",
                source_commit=deployment["source_commit"],
                artifact_digest=deployment["artifact_digest"],
            )


@dataclass(frozen=True)
class TelemetrySnapshot:
    target_id: str
    service: str
    deployment_id: str
    observed_digest: str
    sampled_at: datetime | str
    window_start: datetime | str
    window_end: datetime | str
    request_count: int
    error_count: int
    p95_latency_ms: float
    uptime_ratio: float
    queue_depth: int
    cpu_percent: float
    memory_percent: float
    disk_percent: float


class MonitorRunner(Protocol):
    async def collect_telemetry(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        deployment_id: str,
        expected_digest: str,
        metrics_window: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> TelemetrySnapshot: ...


class MonitoringRecordWriter(Protocol):
    async def write_monitoring_record(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        task_id: str,
        record: dict[str, Any],
        timeout_seconds: float,
    ) -> str: ...


@dataclass(frozen=True)
class MonitorPolicy:
    allowed_targets: Mapping[str, DeploymentTarget]
    max_sample_age_seconds: int = 300

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_targets",
            _validate_target_map(self.allowed_targets, label="Monitor policy"),
        )
        if (
            isinstance(self.max_sample_age_seconds, bool)
            or not isinstance(self.max_sample_age_seconds, int)
            or self.max_sample_age_seconds < 30
            or self.max_sample_age_seconds > 3600
        ):
            raise ValueError("max_sample_age_seconds must be between 30 and 3600")


def _validate_telemetry(
    snapshot: TelemetrySnapshot,
    *,
    target: DeploymentTarget,
    service: str,
    deployment: dict[str, Any],
    max_sample_age_seconds: int,
) -> dict[str, Any]:
    if (
        not isinstance(snapshot, TelemetrySnapshot)
        or snapshot.target_id != target.target_id
        or snapshot.service != service
        or snapshot.deployment_id != deployment["deployment_id"]
        or snapshot.observed_digest != deployment["artifact_digest"]
    ):
        raise ValueError("Monitor telemetry does not match the exact staging deployment")
    sampled = datetime.fromisoformat(
        _timestamp(
            datetime.fromisoformat(snapshot.sampled_at.replace("Z", "+00:00"))
            if isinstance(snapshot.sampled_at, str)
            else snapshot.sampled_at
        ).replace("Z", "+00:00")
    )
    now = datetime.now(timezone.utc)
    if (now - sampled).total_seconds() > max_sample_age_seconds or sampled > now.replace(microsecond=0) + timedelta(seconds=60):
        raise ValueError("Monitor telemetry sample is stale or from the future")
    window_start, window_end = _validate_time_window(snapshot.window_start, snapshot.window_end)
    if datetime.fromisoformat(window_end.replace("Z", "+00:00")) > sampled + timedelta(seconds=60):
        raise ValueError("Monitor telemetry window ends after its sample timestamp")
    counts = (snapshot.request_count, snapshot.error_count, snapshot.queue_depth)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError("Monitor telemetry counts must be non-negative integers")
    if snapshot.error_count > snapshot.request_count:
        raise ValueError("Monitor error count cannot exceed request count")
    metrics = {
        "p95_latency_ms": snapshot.p95_latency_ms,
        "uptime_ratio": snapshot.uptime_ratio,
        "cpu_percent": snapshot.cpu_percent,
        "memory_percent": snapshot.memory_percent,
        "disk_percent": snapshot.disk_percent,
    }
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in metrics.values()
    ):
        raise ValueError("Monitor telemetry contains a non-finite metric")
    if (
        snapshot.p95_latency_ms < 0
        or not 0 <= snapshot.uptime_ratio <= 1
        or any(not 0 <= metrics[key] <= 100 for key in ("cpu_percent", "memory_percent", "disk_percent"))
    ):
        raise ValueError("Monitor telemetry metrics are outside their valid ranges")
    return {
        "target_id": snapshot.target_id,
        "service": snapshot.service,
        "deployment_id": snapshot.deployment_id,
        "observed_digest": snapshot.observed_digest,
        "sampled_at": _timestamp(sampled),
        "window_start": window_start,
        "window_end": window_end,
        "request_count": snapshot.request_count,
        "error_count": snapshot.error_count,
        "error_rate": (
            snapshot.error_count / snapshot.request_count if snapshot.request_count else None
        ),
        **metrics,
        "queue_depth": snapshot.queue_depth,
    }


def _slo_findings(
    tenant_id: str,
    service: str,
    deployment_id: str,
    evidence_ref: str,
    telemetry: dict[str, Any],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    breaches: list[tuple[str, str, float, float, str]] = []
    if telemetry["uptime_ratio"] < profile["availability_target"]:
        breaches.append((
            "monitor.slo_availability_breach",
            "critical" if profile["availability_target"] - telemetry["uptime_ratio"] >= 0.05 else "high",
            telemetry["uptime_ratio"],
            profile["availability_target"],
            "ratio",
        ))
    error_rate = telemetry["error_rate"]
    if error_rate is not None and error_rate > profile["max_error_rate"]:
        threshold = profile["max_error_rate"]
        breaches.append((
            "monitor.slo_error_rate_breach",
            "critical" if error_rate > max(threshold * 2, threshold + 0.01) else "high",
            error_rate,
            threshold,
            "ratio",
        ))
    if telemetry["p95_latency_ms"] > profile["max_p95_latency_ms"]:
        threshold = profile["max_p95_latency_ms"]
        breaches.append((
            "monitor.slo_latency_breach",
            "critical" if telemetry["p95_latency_ms"] > threshold * 2 else "high",
            telemetry["p95_latency_ms"],
            threshold,
            "ms",
        ))
    for key, code, unit in (
        ("queue_depth", "monitor.queue_depth_high", "jobs"),
        ("cpu_percent", "monitor.cpu_utilization_high", "percent"),
        ("memory_percent", "monitor.memory_utilization_high", "percent"),
        ("disk_percent", "monitor.disk_utilization_high", "percent"),
    ):
        observed, threshold = telemetry[key], profile[f"max_{key}"]
        if observed > threshold:
            critical = threshold > 0 and observed > threshold * 2
            breaches.append((code, "critical" if critical else "medium", observed, threshold, unit))
    findings: list[dict[str, Any]] = []
    for code, severity, observed, threshold, unit in breaches:
        correlation_key = hashlib.sha256(
            f"{tenant_id}|{service}|{deployment_id}|{code}".encode("utf-8")
        ).hexdigest()
        findings.append({
            "severity": severity,
            "code": code,
            "message": f"{code} observed {observed:g} {unit}; configured threshold is {threshold:g} {unit}.",
            "evidence_ref": evidence_ref,
            "correlation_key": correlation_key,
        })
    return findings


def _validate_monitor_task(task: dict[str, Any], principal: str) -> None:
    policy = authorize_agent_task(task, authenticated_principal=principal)
    if policy.stage != "monitor":
        raise ValueError("Monitor Agent is only authorized for the Monitor stage")
    if not MONITOR_READ_ONLY_TOOLS.issubset(set(task["constraints"]["allowed_tools"])):
        raise ValueError("Monitor task is missing a read-only telemetry capability")
    _validate_task_network(task)
    if task["constraints"]["write_scope"]:
        raise ValueError("Monitor Agent is read-only; the host persists monitoring records")
    if task["constraints"]["approval_required"] or task["constraints"].get("approval_ref"):
        raise ValueError("Monitor Agent cannot self-authorize operational changes")


class MonitorAgentExecutor:
    """Read telemetry, evaluate SLOs, and emit correlated alerts without service writes."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runner: MonitorRunner,
        record_writer: MonitoringRecordWriter,
        verifier: EvidenceVerifier,
        policy: MonitorPolicy,
    ):
        if not callable(getattr(runner, "collect_telemetry", None)):
            raise TypeError("Monitor Agent requires a telemetry collection adapter")
        if not callable(getattr(record_writer, "write_monitoring_record", None)):
            raise TypeError("Monitor Agent requires a scoped monitoring record writer")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("Monitor Agent requires a trusted deployment evidence verifier")
        self.artifact_store = artifact_store
        self.runner = runner
        self.record_writer = record_writer
        self.verifier = verifier
        self.policy = policy

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None,
    ) -> SpecialistOutcome:
        if authenticated_principal != MONITOR_AGENT:
            raise ExecutionLeaseError("Monitor Agent requires its authenticated workload identity")
        _validate_monitor_task(task, authenticated_principal)
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        service = task["input"]["service"]
        metrics_window = task["input"]["metrics_window"]
        source_commit: str | None = None
        artifact_digest: str | None = None
        try:
            deployment, _ = _verified_deployment_context(
                self.artifact_store,
                task["tenant_id"],
                task["workflow_id"],
                service,
                task["input"]["deployment_evidence_ref"],
                self.verifier,
            )
            source_commit = deployment["source_commit"]
            artifact_digest = deployment["artifact_digest"]
            target = self.policy.allowed_targets.get(deployment["target_id"])
            if target is None or service not in target.allowed_services:
                raise ValueError("deployment target is not in the host-controlled Monitor allowlist")
            if metrics_window not in _METRICS_WINDOWS:
                raise ValueError("metrics_window is not in the supported bounded window set")
            profile = _load_json_artifact(
                self.artifact_store,
                task["tenant_id"],
                task["input"]["slo_profile_ref"],
                label="SLO profile",
            )
            jsonschema.validate(profile, _SLO_PROFILE_SCHEMA)
        except (ArtifactStoreError, ValueError, jsonschema.ValidationError):
            return _failure(
                task,
                started_at=started,
                code="monitor.inputs_invalid",
                message="Monitor Agent could not verify deployment evidence, target, metrics window, or SLO profile.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _timeout_seconds(task)

        def remaining() -> float:
            _checkpoint(checkpoint)
            value = deadline - loop.time()
            if value <= 0:
                raise asyncio.TimeoutError
            return value

        try:
            snapshot = await asyncio.wait_for(
                self.runner.collect_telemetry(
                    target=target,
                    service=service,
                    deployment_id=deployment["deployment_id"],
                    expected_digest=artifact_digest,
                    metrics_window=metrics_window,
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            telemetry = _validate_telemetry(
                snapshot,
                target=target,
                service=service,
                deployment=deployment,
                max_sample_age_seconds=self.policy.max_sample_age_seconds,
            )
            findings = _slo_findings(
                task["tenant_id"],
                service,
                deployment["deployment_id"],
                task["input"]["deployment_evidence_ref"],
                telemetry,
                profile,
            )
            record = {
                "schema_version": 1,
                "type": "rusa.devops.monitoring.record",
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "service": service,
                "target_id": target.target_id,
                "deployment_id": deployment["deployment_id"],
                "source_commit": source_commit,
                "artifact_digest": artifact_digest,
                "deployment_evidence_ref": task["input"]["deployment_evidence_ref"],
                "slo_profile_ref": task["input"]["slo_profile_ref"],
                "metrics_window": metrics_window,
                "telemetry": telemetry,
                "findings": findings,
                "alert_count": len(findings),
                "status": "alert" if findings else "healthy",
                "created_at": _timestamp(),
                "policy_version": task["audit"]["policy_version"],
            }
            report_ref = await asyncio.wait_for(
                self.record_writer.write_monitoring_record(
                    tenant_id=task["tenant_id"],
                    workflow_id=task["workflow_id"],
                    task_id=task["task_id"],
                    record=record,
                    timeout_seconds=remaining(),
                ),
                timeout=remaining(),
            )
            parsed_ref = urlsplit(report_ref) if isinstance(report_ref, str) else None
            if parsed_ref is None or parsed_ref.scheme != "monitoring" or not parsed_ref.netloc:
                raise ValueError("monitoring record writer returned an invalid record reference")
            result = _result(
                task,
                status="passed",
                summary=(
                    f"Telemetry collected and SLO evaluation passed with {len(findings)} alert(s)."
                    if findings
                    else "Telemetry collected and all configured SLO and capacity thresholds passed."
                ),
                artifact_refs=[report_ref],
                evidence_refs=[],
                findings=findings,
                started_at=started,
                handoff=(
                    {"next_agent": OPERATE_AGENT, "event": "monitor.alert_triggered"}
                    if findings
                    else None
                ),
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
            return SpecialistOutcome(result=result)
        except ExecutionLeaseError:
            raise
        except asyncio.TimeoutError:
            return _failure(
                task,
                started_at=started,
                code="monitor.collection_timeout",
                message="Telemetry collection or SLO evaluation exceeded its execution deadline.",
                failure_category="TRANSIENT",
                side_effect="NONE",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        except Exception:
            return _failure(
                task,
                started_at=started,
                code="monitor.collection_invalid",
                message="Monitor Agent received invalid telemetry or could not persist the monitoring record.",
                failure_category="PERMANENT",
                side_effect="NONE",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )


@dataclass(frozen=True)
class SecurityFinding:
    severity: str
    code: str
    message: str
    evidence_ref: str


@dataclass(frozen=True)
class SecurityAssessmentResult:
    scanner_version: str
    findings: tuple[SecurityFinding, ...]


class SecurityAssessmentRunner(Protocol):
    async def assess(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        stage: str,
        evidence_refs: tuple[str, ...],
        evidence_digests: tuple[str, ...],
        source_commit: str | None,
        artifact_digest: str | None,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> SecurityAssessmentResult: ...


@dataclass(frozen=True)
class SecurityGuardPolicy:
    max_critical_findings: int = 0
    max_high_findings: int = 0
    max_evidence_refs: int = 100

    def __post_init__(self) -> None:
        for name, value in (
            ("max_critical_findings", self.max_critical_findings),
            ("max_high_findings", self.max_high_findings),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            isinstance(self.max_evidence_refs, bool)
            or not isinstance(self.max_evidence_refs, int)
            or not 1 <= self.max_evidence_refs <= 500
        ):
            raise ValueError("max_evidence_refs must be between 1 and 500")


def _sanitize_security_message(value: str) -> str:
    message = re.sub(
        r"(?i)\b(password|secret|token|api[_-]?key)\b\s*[:=]\s*[^,\s;]+",
        r"\1=[REDACTED]",
        value,
    )
    message = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED]", message)
    return message[:1000]


def _validate_security_task(task: dict[str, Any], principal: str) -> None:
    policy = authorize_agent_task(task, authenticated_principal=principal)
    if policy.agent_id != SECURITY_GUARD_AGENT:
        raise ValueError("Security Guard task has an invalid workload identity")
    if not READ_ONLY_TOOLS.issubset(set(task["constraints"]["allowed_tools"])):
        raise ValueError("Security Guard task is missing a read-only assessment capability")
    _validate_task_network(task)
    if task["constraints"]["write_scope"]:
        raise ValueError("Security Guard is read-only; the host persists its signed assessment evidence")


class SecurityGuardExecutor:
    """Run a read-only security assessment and append a signed, immutable finding record."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runner: SecurityAssessmentRunner,
        signer: EvidenceSigner,
        policy: SecurityGuardPolicy | None = None,
    ):
        if not callable(getattr(runner, "assess", None)):
            raise TypeError("Security Guard requires a host security assessment adapter")
        if getattr(signer, "producer_id", None) != SECURITY_GUARD_AGENT or not callable(
            getattr(signer, "sign", None)
        ):
            raise ValueError("Security Guard signer must identify rusa_security_guard_agent")
        self.artifact_store = artifact_store
        self.runner = runner
        self.signer = signer
        self.policy = policy or SecurityGuardPolicy()

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None,
    ) -> SpecialistOutcome:
        if authenticated_principal != SECURITY_GUARD_AGENT:
            raise ExecutionLeaseError("Security Guard requires its authenticated workload identity")
        _validate_security_task(task, authenticated_principal)
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        refs = task["input"]["evidence_refs"]
        if (
            not isinstance(refs, list)
            or not refs
            or len(refs) > self.policy.max_evidence_refs
            or len(refs) != len(set(refs))
        ):
            return _failure(
                task,
                started_at=started,
                code="security.evidence_refs_invalid",
                message="Security Guard requires a bounded, unique list of tenant-scoped evidence references.",
                status="blocked",
                failure_category="",
                source_commit=task["input"].get("commit_sha"),
                artifact_digest=task["input"].get("artifact_digest"),
            )
        try:
            digests = tuple(
                hashlib.sha256(self.artifact_store.get(task["tenant_id"], ref)).hexdigest()
                for ref in refs
            )
        except (ArtifactStoreError, ValueError):
            return _failure(
                task,
                started_at=started,
                code="security.evidence_unavailable",
                message="One or more security assessment evidence references are not available to this tenant.",
                status="blocked",
                failure_category="",
                source_commit=task["input"].get("commit_sha"),
                artifact_digest=task["input"].get("artifact_digest"),
            )

        source_commit = task["input"].get("commit_sha")
        artifact_digest = task["input"].get("artifact_digest")
        if source_commit is not None and not re.fullmatch(r"[a-f0-9]{40}", source_commit):
            return _failure(
                task,
                started_at=started,
                code="security.source_commit_invalid",
                message="Security Guard received an invalid source commit reference.",
                status="blocked",
                failure_category="",
            )
        if artifact_digest is not None and not re.fullmatch(r"sha256:[a-f0-9]{64}", artifact_digest):
            return _failure(
                task,
                started_at=started,
                code="security.artifact_digest_invalid",
                message="Security Guard received an invalid artifact digest.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _timeout_seconds(task)

        def remaining() -> float:
            _checkpoint(checkpoint)
            value = deadline - loop.time()
            if value <= 0:
                raise asyncio.TimeoutError
            return value

        try:
            assessment = await asyncio.wait_for(
                self.runner.assess(
                    tenant_id=task["tenant_id"],
                    workflow_id=task["workflow_id"],
                    stage=task["stage"],
                    evidence_refs=tuple(refs),
                    evidence_digests=digests,
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            if (
                not isinstance(assessment, SecurityAssessmentResult)
                or not isinstance(assessment.scanner_version, str)
                or not 1 <= len(assessment.scanner_version) <= 128
            ):
                raise ValueError("security scanner returned an invalid assessment envelope")
            findings: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for item in assessment.findings:
                if (
                    not isinstance(item, SecurityFinding)
                    or item.severity not in {"critical", "high", "medium", "low", "info"}
                    or not _CODE_RE.fullmatch(item.code)
                    or not isinstance(item.message, str)
                    or not item.message.strip()
                    or item.evidence_ref not in refs
                ):
                    raise ValueError("security scanner returned an invalid or unbound finding")
                identity = (item.code, item.evidence_ref)
                if identity in seen:
                    continue
                seen.add(identity)
                findings.append({
                    "severity": item.severity,
                    "code": item.code,
                    "message": _sanitize_security_message(item.message),
                    "evidence_ref": item.evidence_ref,
                })
            critical = sum(1 for item in findings if item["severity"] == "critical")
            high = sum(1 for item in findings if item["severity"] == "high")
            status = (
                "blocked"
                if critical > self.policy.max_critical_findings
                or high > self.policy.max_high_findings
                else "passed"
            )
            created_at = _timestamp()
            payload: dict[str, Any] = {
                "schema_version": 1,
                "type": "rusa.devops.security.attestation",
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "agent_id": SECURITY_GUARD_AGENT,
                "stage": task["stage"],
                "evidence_refs": refs,
                "evidence_digests": list(digests),
                "findings": findings,
                "critical_count": critical,
                "high_count": high,
                "scanner_version": assessment.scanner_version,
                "status": status,
                "created_at": created_at,
                "policy_version": task["audit"]["policy_version"],
            }
            if source_commit is not None:
                payload["source_commit"] = source_commit
            if artifact_digest is not None:
                payload["artifact_digest"] = artifact_digest
            evidence_ref = self.artifact_store.put_json(
                task["tenant_id"],
                {
                    "schema_version": 1,
                    "producer_id": SECURITY_GUARD_AGENT,
                    "payload": payload,
                    "signature": self.signer.sign(
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        ).encode("utf-8")
                    ),
                },
                namespace="evidence",
            )
            _checkpoint(checkpoint)
            result = _result(
                task,
                status=status,
                summary=(
                    f"Security assessment passed with {critical} critical and {high} high finding(s)."
                    if status == "passed"
                    else f"Security Guard blocked the workflow with {critical} critical and {high} high finding(s)."
                ),
                artifact_refs=[],
                evidence_refs=[evidence_ref],
                findings=findings,
                started_at=started,
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
            return SpecialistOutcome(
                result=result,
                failure_category="" if status == "passed" else "PERMANENT",
                side_effect="APPLIED",
            )
        except ExecutionLeaseError:
            raise
        except asyncio.TimeoutError:
            return _failure(
                task,
                started_at=started,
                code="security.assessment_timeout",
                message="Security assessment exceeded its execution deadline; no signed assessment was produced.",
                failure_category="TRANSIENT",
                side_effect="NONE",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        except Exception:
            return _failure(
                task,
                started_at=started,
                code="security.assessment_invalid",
                message="Security Guard could not validate the scanner result or publish its signed assessment.",
                failure_category="PERMANENT",
                side_effect="UNKNOWN",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
