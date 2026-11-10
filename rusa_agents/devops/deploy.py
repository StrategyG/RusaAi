"""Staging-only deployment with signed release verification and rollback rehearsal.

The deployment runner is an injected host adapter. Task payloads cannot provide
commands, URLs, credentials, or arbitrary deployment destinations.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Protocol

import jsonschema

from .agent_registry import DEPLOY_AGENT, OPERATE_AGENT, RELEASE_AGENT, authorize_agent_task
from .artifacts import ArtifactStoreError, ContentAddressedArtifactStore
from .build_test import (
    EvidenceSigner,
    EvidenceVerifier,
    _aware_timestamp,
    _load_json_artifact,
    _signed_evidence,
    _validate_oci_reference,
    _validate_registry_hosts,
    _validate_time_window,
    _verified_evidence,
)
from .release import ApprovalDecision, ApprovalVerifier
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

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_RELEASE_VERSION_RE = re.compile(
    r"^(?:v)?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?$"
)
_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_HEALTH_CHECK_IDS = frozenset({"http_health", "dependency_health"})
_SMOKE_CHECK_IDS = frozenset(
    {"api_smoke", "database_readiness", "queue_readiness", "inference_smoke"}
)

_SMOKE_TEST_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "network_access", "checks"],
    "properties": {
        "schema_version": {"const": 1},
        "network_access": {"const": False},
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "required"],
                "properties": {
                    "id": {"enum": sorted(_SMOKE_CHECK_IDS)},
                    "required": {"type": "boolean"},
                },
            },
        },
    },
}

_ROLLBACK_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "strategy", "expected_previous_digest", "max_recovery_seconds"],
    "properties": {
        "schema_version": {"const": 1},
        "strategy": {"const": "previous_digest"},
        "expected_previous_digest": {"type": "string", "pattern": _SHA256_RE.pattern},
        "max_recovery_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
    },
}


@dataclass(frozen=True)
class DeploymentTarget:
    """Host-controlled staging target; it deliberately contains no credentials."""

    target_id: str
    environment: str
    allowed_services: frozenset[str]


@dataclass(frozen=True)
class DeployPolicy:
    """Host-controlled deployment policy; task input cannot widen it."""

    allowed_targets: Mapping[str, DeploymentTarget]
    require_staging_approval: bool = True
    required_health_checks: frozenset[str] = frozenset({"http_health"})
    required_smoke_checks: frozenset[str] = frozenset({"api_smoke"})
    auto_rollback_on_failed_verification: bool = False

    def __post_init__(self) -> None:
        targets = dict(self.allowed_targets)
        if not targets:
            raise ValueError("Deploy policy requires at least one allowlisted staging target")
        for target_id, target in targets.items():
            if (
                not isinstance(target, DeploymentTarget)
                or target_id != target.target_id
                or not _TARGET_ID_RE.fullmatch(target_id)
                or target.environment != "staging"
                or not target.allowed_services
                or any(not _SERVICE_RE.fullmatch(service) for service in target.allowed_services)
            ):
                raise ValueError("Deploy policy contains an invalid or non-staging target")
        if type(self.require_staging_approval) is not bool:
            raise ValueError("require_staging_approval must be a boolean")
        if type(self.auto_rollback_on_failed_verification) is not bool:
            raise ValueError("auto_rollback_on_failed_verification must be a boolean")
        if (
            not self.required_health_checks
            or not self.required_health_checks.issubset(_HEALTH_CHECK_IDS)
            or not self.required_smoke_checks
            or not self.required_smoke_checks.issubset(_SMOKE_CHECK_IDS)
        ):
            raise ValueError("Deploy policy contains unsupported or empty required check sets")
        object.__setattr__(self, "allowed_targets", MappingProxyType(targets))


@dataclass(frozen=True)
class DeploymentSnapshot:
    target_id: str
    service: str
    current_digest: str


@dataclass(frozen=True)
class DeploymentExecutionResult:
    deployment_id: str
    target_id: str
    service: str
    artifact_digest: str
    previous_digest: str
    started_at: datetime | str
    completed_at: datetime | str


@dataclass(frozen=True)
class DeploymentCheckResult:
    check_id: str
    status: str
    duration_seconds: float


@dataclass(frozen=True)
class DeploymentVerificationResult:
    target_id: str
    service: str
    deployment_id: str
    observed_digest: str
    checks: tuple[DeploymentCheckResult, ...]
    started_at: datetime | str
    completed_at: datetime | str


@dataclass(frozen=True)
class RollbackRehearsalResult:
    target_id: str
    service: str
    validated_previous_digest: str
    status: str
    started_at: datetime | str
    completed_at: datetime | str


@dataclass(frozen=True)
class RollbackExecutionResult:
    target_id: str
    service: str
    deployment_id: str
    restored_digest: str
    status: str
    started_at: datetime | str
    completed_at: datetime | str


class DeploymentRunner(Protocol):
    """Privileged host adapter; implementation owns network and secret access."""

    async def inspect_current(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> DeploymentSnapshot: ...

    async def rehearse_rollback(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        previous_digest: str,
        rollback_plan: dict[str, Any],
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> RollbackRehearsalResult: ...

    async def deploy(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        release_manifest: dict[str, Any],
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> DeploymentExecutionResult: ...

    async def health_check(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        deployment_id: str,
        expected_digest: str,
        check_ids: tuple[str, ...],
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> DeploymentVerificationResult: ...

    async def smoke_test(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        deployment_id: str,
        expected_digest: str,
        test_plan: dict[str, Any],
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> DeploymentVerificationResult: ...

    async def rollback(
        self,
        *,
        target: DeploymentTarget,
        service: str,
        deployment_id: str,
        previous_digest: str,
        rollback_plan: dict[str, Any],
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> RollbackExecutionResult: ...


def _validate_deploy_task(task: dict[str, Any], *, principal: str) -> None:
    policy = authorize_agent_task(task, authenticated_principal=principal)
    if policy.stage != "deploy":
        raise ValueError("Deploy Agent is only authorized for the Deploy stage")
    constraints = task["constraints"]
    required_tools = {
        "artifact.read",
        "release.read",
        "deployment.execute",
        "health.read",
        "runbook.read",
        "evidence.write",
    }
    if not required_tools.issubset(set(constraints["allowed_tools"])):
        raise ValueError("Deploy task is missing a required release, deployment, or evidence capability")
    if constraints["network_access"] or constraints.get("allowed_network_destinations"):
        raise ValueError("Deploy Agent cannot receive task-controlled network access")
    if not {"deployments/", "evidence/"}.issubset(set(constraints["write_scope"])):
        raise ValueError("Deploy task write scope does not cover deployment and evidence records")
    if task["input"].get("environment") != "staging":
        raise ValueError("this Deploy Agent increment supports staging only")
    if constraints["approval_required"]:
        if set(constraints.get("approval_scope", [])) != {"deploy"}:
            raise ValueError("Deploy approval must be scoped only to deploy")
    elif constraints.get("approval_scope") or constraints.get("approval_ref"):
        raise ValueError("Deploy approval scope/reference cannot be supplied when approval is not required")


def _smoke_test_plan(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
    required_checks: frozenset[str],
) -> dict[str, Any]:
    plan = _load_json_artifact(store, tenant_id, reference, label="smoke test plan")
    jsonschema.validate(plan, _SMOKE_TEST_PLAN_SCHEMA)
    rows = plan["checks"]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("smoke test plan contains duplicate check IDs")
    by_id = {row["id"]: row for row in rows}
    if not required_checks.issubset(by_id) or any(
        by_id[check_id]["required"] is not True for check_id in required_checks
    ):
        raise ValueError("smoke test plan omits a host-required check or marks it optional")
    return plan


def _rollback_plan(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
) -> dict[str, Any]:
    plan = _load_json_artifact(store, tenant_id, reference, label="rollback plan")
    jsonschema.validate(plan, _ROLLBACK_PLAN_SCHEMA)
    return plan


def _verify_release_candidate(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    workflow_id: str,
    source_commit: str,
    artifact_digest: str,
    manifest_ref: str,
    evidence_ref: str,
    verifier: EvidenceVerifier,
    allowed_registry_hosts: frozenset[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = _verified_evidence(
        store,
        tenant_id,
        evidence_ref,
        verifier=verifier,
        expected_producer=RELEASE_AGENT,
        expected_type="rusa.devops.release.attestation",
    )
    if any(
        evidence.get(key) != expected
        for key, expected in (
            ("tenant_id", tenant_id),
            ("workflow_id", workflow_id),
            ("source_commit", source_commit),
            ("artifact_digest", artifact_digest),
            ("release_manifest_ref", manifest_ref),
            ("agent_id", RELEASE_AGENT),
        )
    ):
        raise ValueError("signed Release evidence does not match this tenant, workflow, commit, or digest")
    raw_manifest = store.get(tenant_id, manifest_ref)
    manifest_digest = hashlib.sha256(raw_manifest).hexdigest()
    if evidence.get("release_manifest_digest") != manifest_digest:
        raise ValueError("release manifest bytes do not match the signed Release digest")
    manifest = _load_json_artifact(store, tenant_id, manifest_ref, label="release manifest")
    if any(
        manifest.get(key) != expected
        for key, expected in (
            ("kind", "rusa.release.candidate"),
            ("status", "candidate"),
            ("release_channel", "candidate"),
            ("release_version", evidence.get("release_version")),
            ("source_commit", source_commit),
            ("artifact_digest", artifact_digest),
            ("build_evidence_ref", evidence.get("build_evidence_ref")),
            ("test_evidence_ref", evidence.get("test_evidence_ref")),
            ("sbom_ref", evidence.get("sbom_ref")),
            ("sbom_digest", evidence.get("sbom_digest")),
        )
    ):
        raise ValueError("release candidate manifest does not match its signed attestation")
    if (
        not isinstance(evidence.get("release_version"), str)
        or not _RELEASE_VERSION_RE.fullmatch(evidence["release_version"])
        or not isinstance(manifest.get("repository"), str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["repository"])
    ):
        raise ValueError("release candidate version or repository identity is invalid")
    _validate_oci_reference(manifest.get("artifact_ref"), artifact_digest, allowed_registry_hosts)
    return evidence, manifest


def _verify_approval(
    verifier: ApprovalVerifier,
    approval_ref: str,
    *,
    stage: str = "deploy",
    tenant_id: str,
    workflow_id: str,
    source_commit: str,
    artifact_digest: str,
) -> str:
    decision = verifier.verify_approval(
        approval_ref,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        stage=stage,
        source_commit=source_commit,
        artifact_digest=artifact_digest,
    )
    if (
        not isinstance(decision, ApprovalDecision)
        or decision.approved is not True
        or decision.tenant_id != tenant_id
        or decision.workflow_id != workflow_id
        or decision.stage != stage
        or decision.source_commit != source_commit
        or decision.artifact_digest != artifact_digest
        or not isinstance(decision.approval_id, str)
        or not decision.approval_id
        or not isinstance(decision.approver_id, str)
        or not decision.approver_id
    ):
        raise ValueError("approval decision is not bound to this staging deployment")
    issued = datetime.fromisoformat(
        _aware_timestamp(decision.issued_at, label="approval issued_at").replace("Z", "+00:00")
    )
    expires = datetime.fromisoformat(
        _aware_timestamp(decision.expires_at, label="approval expires_at").replace("Z", "+00:00")
    )
    now = datetime.now(timezone.utc)
    if issued > now or expires <= now or expires <= issued:
        raise ValueError("deployment approval is expired or not yet valid")
    return decision.approval_id


def _validate_verification(
    execution: DeploymentVerificationResult,
    *,
    target: DeploymentTarget,
    service: str,
    deployment_id: str,
    artifact_digest: str,
    expected_checks: frozenset[str],
    required_checks: frozenset[str],
    label: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(execution, DeploymentVerificationResult):
        raise ValueError(f"{label} runner returned an unsupported result type")
    if (
        execution.target_id != target.target_id
        or execution.service != service
        or execution.deployment_id != deployment_id
        or execution.observed_digest != artifact_digest
    ):
        raise ValueError(f"{label} result does not attest the exact deployed target and digest")
    started, completed = _validate_time_window(execution.started_at, execution.completed_at)
    actual: dict[str, DeploymentCheckResult] = {}
    for check in execution.checks:
        if not isinstance(check, DeploymentCheckResult) or check.check_id not in expected_checks:
            raise ValueError(f"{label} result contains an unknown or invalid check")
        if check.check_id in actual:
            raise ValueError(f"{label} result contains a duplicate check")
        if check.status not in {"passed", "failed", "skipped"}:
            raise ValueError(f"{label} result contains an invalid check status")
        if (
            isinstance(check.duration_seconds, bool)
            or not isinstance(check.duration_seconds, (int, float))
            or not math.isfinite(check.duration_seconds)
            or check.duration_seconds < 0
        ):
            raise ValueError(f"{label} check duration is invalid")
        actual[check.check_id] = check
    if set(actual) != set(expected_checks):
        raise ValueError(f"{label} runner did not return exactly the requested checks")
    findings: list[dict[str, str]] = []
    for check_id, check in actual.items():
        if check.status == "failed":
            findings.append({
                "severity": "high",
                "code": f"deploy.{label}_check_failed",
                "message": f"{label} check {check_id} failed.",
            })
        elif check.status == "skipped" and check_id in required_checks:
            findings.append({
                "severity": "high",
                "code": f"deploy.required_{label}_check_skipped",
                "message": f"Required {label} check {check_id} was skipped.",
            })
    return {
        "target_id": execution.target_id,
        "service": execution.service,
        "deployment_id": execution.deployment_id,
        "observed_digest": execution.observed_digest,
        "started_at": started,
        "completed_at": completed,
        "checks": [asdict(actual[check_id]) for check_id in sorted(actual)],
    }, findings


class DeployAgentExecutor:
    """Verify a signed release candidate and deploy it to an allowlisted staging target."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runner: DeploymentRunner,
        signer: EvidenceSigner,
        verifier: EvidenceVerifier,
        allowed_registry_hosts: set[str] | frozenset[str],
        policy: DeployPolicy,
        approval_verifier: ApprovalVerifier | None = None,
    ):
        required_methods = (
            "inspect_current",
            "rehearse_rollback",
            "deploy",
            "health_check",
            "smoke_test",
        )
        if any(not callable(getattr(runner, name, None)) for name in required_methods):
            raise TypeError("Deploy Agent runner is missing a required staging adapter method")
        if policy.auto_rollback_on_failed_verification and not callable(
            getattr(runner, "rollback", None)
        ):
            raise TypeError("automatic rollback policy requires a rollback-capable runner")
        if getattr(signer, "producer_id", None) != DEPLOY_AGENT or not callable(
            getattr(signer, "sign", None)
        ):
            raise ValueError("Deploy Agent signer must identify rusa_deploy_agent")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("Deploy Agent requires a trusted evidence verifier")
        self.artifact_store = artifact_store
        self.runner = runner
        self.signer = signer
        self.verifier = verifier
        self.allowed_registry_hosts = _validate_registry_hosts(allowed_registry_hosts)
        self.policy = policy
        self.approval_verifier = approval_verifier

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None,
    ) -> SpecialistOutcome:
        if authenticated_principal != DEPLOY_AGENT:
            raise ExecutionLeaseError("Deploy Agent requires its authenticated workload identity")
        _validate_deploy_task(task, principal=authenticated_principal)
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        source_commit = task["input"]["commit_sha"]
        artifact_digest = task["input"]["artifact_digest"]

        approval_ref = task["constraints"].get("approval_ref")
        approval_required = (
            self.policy.require_staging_approval or task["constraints"]["approval_required"]
        )
        approval_id: str | None = None
        if approval_required:
            if not approval_ref:
                result = _result(
                    task,
                    status="needs_approval",
                    summary="The staging deployment is waiting for the required human approval.",
                    artifact_refs=[],
                    evidence_refs=[],
                    findings=[{
                        "severity": "info",
                        "code": "deploy.approval_required",
                        "message": "A deployment approval bound to this release candidate is required.",
                    }],
                    started_at=started,
                    approval_ref=f"approval://pending/deploy/{task['task_id']}",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )
                return SpecialistOutcome(result=result)
            if self.approval_verifier is None:
                return _failure(
                    task,
                    started_at=started,
                    code="deploy.approval_verifier_unavailable",
                    message="Staging deployment requires a trusted host approval verifier.",
                    status="blocked",
                    failure_category="",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )
            try:
                approval_id = _verify_approval(
                    self.approval_verifier,
                    approval_ref,
                    tenant_id=task["tenant_id"],
                    workflow_id=task["workflow_id"],
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )
            except Exception:
                return _failure(
                    task,
                    started_at=started,
                    code="deploy.approval_invalid",
                    message="Deployment approval is invalid or not bound to this tenant, workflow, commit, and digest.",
                    status="blocked",
                    failure_category="",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )

        target_id = task["input"]["target_id"]
        service = task["input"]["service"]
        target = self.policy.allowed_targets.get(target_id)
        if target is None or service not in target.allowed_services:
            return _failure(
                task,
                started_at=started,
                code="deploy.target_not_allowlisted",
                message="The requested staging target or service is not in the host-controlled deployment allowlist.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )

        try:
            _, manifest = _verify_release_candidate(
                self.artifact_store,
                task["tenant_id"],
                task["workflow_id"],
                source_commit,
                artifact_digest,
                task["input"]["release_manifest_ref"],
                task["input"]["release_evidence_ref"],
                self.verifier,
                self.allowed_registry_hosts,
            )
            smoke_plan = _smoke_test_plan(
                self.artifact_store,
                task["tenant_id"],
                task["input"]["smoke_test_plan_ref"],
                self.policy.required_smoke_checks,
            )
            rollback_plan = _rollback_plan(
                self.artifact_store,
                task["tenant_id"],
                task["input"]["rollback_plan_ref"],
            )
        except (ArtifactStoreError, ValueError, jsonschema.ValidationError):
            return _failure(
                task,
                started_at=started,
                code="deploy.release_or_plan_invalid",
                message="Deploy Agent could not verify the signed release candidate, smoke plan, or rollback plan.",
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

        def validate_snapshot(snapshot: DeploymentSnapshot) -> None:
            if (
                not isinstance(snapshot, DeploymentSnapshot)
                or snapshot.target_id != target.target_id
                or snapshot.service != service
                or not _SHA256_RE.fullmatch(snapshot.current_digest)
            ):
                raise ValueError("deployment target inspection returned invalid state")

        deployment_attempted = False
        deployment: DeploymentExecutionResult | None = None
        try:
            snapshot = await asyncio.wait_for(
                self.runner.inspect_current(
                    target=target,
                    service=service,
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            validate_snapshot(snapshot)
            previous_digest = rollback_plan["expected_previous_digest"]
            if snapshot.current_digest != previous_digest:
                return _failure(
                    task,
                    started_at=started,
                    code="deploy.rollback_target_mismatch",
                    message="The current staging digest does not match the approved rollback target.",
                    status="blocked",
                    failure_category="",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )

            rehearsal = await asyncio.wait_for(
                self.runner.rehearse_rollback(
                    target=target,
                    service=service,
                    previous_digest=previous_digest,
                    rollback_plan=rollback_plan,
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            if (
                not isinstance(rehearsal, RollbackRehearsalResult)
                or rehearsal.target_id != target.target_id
                or rehearsal.service != service
                or rehearsal.validated_previous_digest != previous_digest
                or rehearsal.status != "passed"
            ):
                return _failure(
                    task,
                    started_at=started,
                    code="deploy.rollback_rehearsal_failed",
                    message="The staging rollback rehearsal did not verify the approved previous digest.",
                    status="blocked",
                    failure_category="",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )
            rehearsal_started, rehearsal_completed = _validate_time_window(
                rehearsal.started_at, rehearsal.completed_at
            )

            deployment_attempted = True
            deployment = await asyncio.wait_for(
                self.runner.deploy(
                    target=target,
                    service=service,
                    release_manifest=manifest,
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            if (
                not isinstance(deployment, DeploymentExecutionResult)
                or not isinstance(deployment.deployment_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", deployment.deployment_id)
                or deployment.target_id != target.target_id
                or deployment.service != service
                or deployment.artifact_digest != artifact_digest
                or deployment.previous_digest != previous_digest
            ):
                return _failure(
                    task,
                    started_at=started,
                    code="deploy.execution_target_mismatch",
                    message="The deployment adapter did not confirm the exact allowlisted target and immutable artifact digest.",
                    failure_category="AMBIGUOUS",
                    side_effect="UNKNOWN",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )
            deployment_started, deployment_completed = _validate_time_window(
                deployment.started_at, deployment.completed_at
            )

            health = await asyncio.wait_for(
                self.runner.health_check(
                    target=target,
                    service=service,
                    deployment_id=deployment.deployment_id,
                    expected_digest=artifact_digest,
                    check_ids=tuple(sorted(self.policy.required_health_checks)),
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            health_record, health_findings = _validate_verification(
                health,
                target=target,
                service=service,
                deployment_id=deployment.deployment_id,
                artifact_digest=artifact_digest,
                expected_checks=self.policy.required_health_checks,
                required_checks=self.policy.required_health_checks,
                label="health",
            )
            smoke = await asyncio.wait_for(
                self.runner.smoke_test(
                    target=target,
                    service=service,
                    deployment_id=deployment.deployment_id,
                    expected_digest=artifact_digest,
                    test_plan=smoke_plan,
                    timeout_seconds=remaining(),
                    checkpoint=checkpoint,
                ),
                timeout=remaining(),
            )
            smoke_ids = frozenset(row["id"] for row in smoke_plan["checks"])
            smoke_required = frozenset(
                row["id"] for row in smoke_plan["checks"] if row["required"] is True
            )
            smoke_record, smoke_findings = _validate_verification(
                smoke,
                target=target,
                service=service,
                deployment_id=deployment.deployment_id,
                artifact_digest=artifact_digest,
                expected_checks=smoke_ids,
                required_checks=smoke_required,
                label="smoke",
            )
            findings = health_findings + smoke_findings
            rollback_record: dict[str, Any] | None = None
            rollback_side_effect = "APPLIED"
            if findings and self.policy.auto_rollback_on_failed_verification:
                rollback = await asyncio.wait_for(
                    self.runner.rollback(
                        target=target,
                        service=service,
                        deployment_id=deployment.deployment_id,
                        previous_digest=previous_digest,
                        rollback_plan=rollback_plan,
                        timeout_seconds=remaining(),
                        checkpoint=checkpoint,
                    ),
                    timeout=remaining(),
                )
                if (
                    not isinstance(rollback, RollbackExecutionResult)
                    or rollback.target_id != target.target_id
                    or rollback.service != service
                    or rollback.deployment_id != deployment.deployment_id
                    or rollback.restored_digest != previous_digest
                    or rollback.status != "passed"
                ):
                    rollback_side_effect = "UNKNOWN"
                    findings.append({
                        "severity": "critical",
                        "code": "deploy.rollback_failed",
                        "message": "Automatic staging rollback did not confirm restoration of the previous digest.",
                    })
                else:
                    rollback_started, rollback_completed = _validate_time_window(
                        rollback.started_at, rollback.completed_at
                    )
                    restored = await asyncio.wait_for(
                        self.runner.inspect_current(
                            target=target,
                            service=service,
                            timeout_seconds=remaining(),
                            checkpoint=checkpoint,
                        ),
                        timeout=remaining(),
                    )
                    validate_snapshot(restored)
                    if restored.current_digest != previous_digest:
                        rollback_side_effect = "UNKNOWN"
                        findings.append({
                            "severity": "critical",
                            "code": "deploy.rollback_state_unverified",
                            "message": "Rollback reported success but the staging target does not show the previous digest.",
                        })
                    else:
                        rollback_record = {
                            "status": "passed",
                            "restored_digest": rollback.restored_digest,
                            "started_at": rollback_started,
                            "completed_at": rollback_completed,
                        }

            status = "passed" if not findings else "failed"
            created_at = _timestamp()
            deployment_record = {
                "schema_version": 1,
                "type": "rusa.devops.deployment.record",
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "environment": "staging",
                "target_id": target.target_id,
                "service": service,
                "release_version": manifest["release_version"],
                "release_manifest_ref": task["input"]["release_manifest_ref"],
                "release_evidence_ref": task["input"]["release_evidence_ref"],
                "source_commit": source_commit,
                "artifact_ref": manifest["artifact_ref"],
                "artifact_digest": artifact_digest,
                "deployment_id": deployment.deployment_id,
                "previous_digest": previous_digest,
                "deployment_started_at": deployment_started,
                "deployment_completed_at": deployment_completed,
                "health": health_record,
                "smoke": smoke_record,
                "rollback_plan_ref": task["input"]["rollback_plan_ref"],
                "rollback_rehearsal": {
                    "status": rehearsal.status,
                    "validated_previous_digest": rehearsal.validated_previous_digest,
                    "started_at": rehearsal_started,
                    "completed_at": rehearsal_completed,
                },
                "rollback_execution": rollback_record,
                "approval_ref": approval_ref,
                "approval_id": approval_id,
                "status": status,
                "created_at": created_at,
                "policy_version": task["audit"]["policy_version"],
            }
            record_ref = self.artifact_store.put_json(
                task["tenant_id"], deployment_record, namespace="artifact"
            )
            record_digest = record_ref.rsplit("/", 1)[-1]
            evidence_payload = {
                "schema_version": 1,
                "type": "rusa.devops.deploy.attestation",
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "agent_id": DEPLOY_AGENT,
                "environment": "staging",
                "target_id": target.target_id,
                "service": service,
                "release_version": manifest["release_version"],
                "release_manifest_ref": task["input"]["release_manifest_ref"],
                "release_evidence_ref": task["input"]["release_evidence_ref"],
                "deployment_record_ref": record_ref,
                "deployment_record_digest": record_digest,
                "source_commit": source_commit,
                "artifact_digest": artifact_digest,
                "deployment_id": deployment.deployment_id,
                "previous_digest": previous_digest,
                "status": status,
                "approval_id": approval_id,
                "created_at": created_at,
                "policy_version": task["audit"]["policy_version"],
            }
            evidence_ref = self.artifact_store.put_json(
                task["tenant_id"],
                _signed_evidence(
                    payload=evidence_payload,
                    signer=self.signer,
                    expected_producer=DEPLOY_AGENT,
                ),
                namespace="evidence",
            )
            _checkpoint(checkpoint)
            result = _result(
                task,
                status=status,
                summary=(
                    "The signed release candidate was deployed to the allowlisted staging target; health, smoke, and rollback rehearsal passed."
                    if status == "passed"
                    else "Staging deployment verification failed; the release is not eligible for operational handoff."
                ),
                artifact_refs=[record_ref],
                evidence_refs=[evidence_ref],
                findings=findings,
                started_at=started,
                handoff=(
                    {"next_agent": OPERATE_AGENT, "event": "deploy.completed"}
                    if status == "passed"
                    else None
                ),
                source_commit=source_commit,
                artifact_digest=artifact_digest,
                approval_ref=approval_ref,
            )
            return SpecialistOutcome(
                result=result,
                failure_category="" if status == "passed" else "PERMANENT",
                side_effect=rollback_side_effect,
            )
        except ExecutionLeaseError:
            raise
        except asyncio.TimeoutError:
            return _failure(
                task,
                started_at=started,
                code="deploy.execution_timeout",
                message="Staging deployment exceeded its execution deadline; inspect the target before retrying.",
                failure_category="AMBIGUOUS" if deployment_attempted else "TRANSIENT",
                side_effect="UNKNOWN" if deployment_attempted else "NONE",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        except Exception:
            return _failure(
                task,
                started_at=started,
                code="deploy.execution_failed",
                message="Deploy Agent could not complete and attest the staging deployment.",
                failure_category="AMBIGUOUS" if deployment_attempted else "PERMANENT",
                side_effect="UNKNOWN" if deployment_attempted else "NONE",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
