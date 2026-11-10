"""Claim-bound release candidate and staging deployment specialists.

Release and deployment side effects are injected host adapters. This module
never executes task-provided shell commands, accesses raw secrets, or deploys
outside the configured staging target.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

import jsonschema

from .agent_registry import DEPLOY_AGENT, OPERATE_AGENT, RELEASE_AGENT, authorize_agent_task
from .artifacts import ArtifactStoreError, ContentAddressedArtifactStore
from .build_test import (
    BUILD_AGENT,
    TEST_AGENT,
    EvidenceSigner,
    EvidenceVerifier,
    _load_json_artifact,
    _signed_evidence,
    _validate_oci_reference,
    _validate_registry_hosts,
    _validate_sbom,
    _validate_time_window,
    _verified_evidence,
)
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


class ApprovalVerifier(Protocol):
    """Host-side verifier for signed, scoped human approval records."""

    async def verify(
        self,
        *,
        reference: str,
        scope: str,
        tenant_id: str,
        workflow_id: str,
        resource_ref: str,
    ) -> bool: ...


class DeploymentRunner(Protocol):
    """Isolated staging adapter; target credentials remain outside the agent."""

    async def deploy(
        self,
        *,
        target_id: str,
        service: str,
        environment: str,
        release_manifest_ref: str,
        artifact_ref: str,
        artifact_digest: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> DeploymentExecutionResult: ...

    async def health_check(
        self,
        *,
        target_id: str,
        service: str,
        deployment_id: str,
        artifact_digest: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> HealthCheckResult: ...

    async def rehearse_rollback(
        self,
        *,
        target_id: str,
        service: str,
        deployment_id: str,
        rollback_plan_ref: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> RollbackRehearsalResult: ...


@dataclass(frozen=True)
class DeploymentExecutionResult:
    deployment_id: str
    target_id: str
    service: str
    environment: str
    release_manifest_ref: str
    artifact_digest: str
    started_at: datetime | str
    completed_at: datetime | str


@dataclass(frozen=True)
class HealthProbeResult:
    name: str
    passed: bool
    details: str = ""


@dataclass(frozen=True)
class HealthCheckResult:
    deployment_id: str
    healthy: bool
    smoke_tests_passed: bool
    checks: tuple[HealthProbeResult, ...]
    checked_at: datetime | str


@dataclass(frozen=True)
class RollbackRehearsalResult:
    deployment_id: str
    rollback_plan_ref: str
    passed: bool
    previous_artifact_digest: str | None
    completed_at: datetime | str


_RELEASE_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "version",
        "service",
        "changelog",
        "deployment_notes",
        "migration_notes",
        "migration_compatible",
        "known_issues",
        "rollback_notes",
        "release_checklist",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "version": {
            "type": "string",
            "pattern": r"^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\\+[0-9A-Za-z.-]+)?$",
            "maxLength": 128,
        },
        "service": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"},
        "changelog": {"type": "string", "minLength": 1, "maxLength": 20000},
        "deployment_notes": {"type": "string", "minLength": 1, "maxLength": 10000},
        "migration_notes": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "migration_compatible": {"type": "boolean"},
        "known_issues": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "rollback_notes": {"type": "string", "minLength": 1, "maxLength": 10000},
        "release_checklist": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "required", "status"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "required": {"type": "boolean"},
                    "status": {"enum": ["passed", "failed"]},
                    "evidence_ref": {"type": "string", "format": "uri"},
                },
            },
        },
    },
}

_ROLLBACK_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "service",
        "target_id",
        "strategy",
        "trigger_conditions",
        "approval_ref",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "service": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"},
        "target_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"},
        "strategy": {"enum": ["previous_digest", "blue_green", "rolling_rollback"]},
        "trigger_conditions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "approval_ref": {"type": "string", "format": "uri"},
        "previous_artifact_digest": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
    },
}

_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_APPROVAL_REF_RE = re.compile(r"^approval://[A-Za-z0-9._/-]{1,240}$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_release_deploy_task(
    task: dict[str, Any],
    *,
    principal: str,
    stage: str,
    required_tools: frozenset[str],
    required_write_scopes: frozenset[str],
) -> None:
    policy = authorize_agent_task(task, authenticated_principal=principal)
    if policy.stage != stage:
        raise ValueError("specialist identity does not match its lifecycle stage")
    constraints = task["constraints"]
    if not required_tools.issubset(set(constraints["allowed_tools"])):
        raise ValueError("task is missing a required specialist capability")
    if not required_write_scopes.issubset(set(constraints["write_scope"])):
        raise ValueError("task write scope does not cover specialist output paths")
    if stage == "release" and (
        constraints["network_access"] or constraints.get("allowed_network_destinations")
    ):
        raise ValueError("Release Agent must not receive network access")


def _approval_request_ref(task: dict[str, Any], scope: str) -> str:
    return f"approval://requests/{task['workflow_id']}/{task['task_id']}/{scope}"


def _validate_approval_reference(reference: str) -> None:
    if not isinstance(reference, str) or not _APPROVAL_REF_RE.fullmatch(reference):
        raise ValueError("approval reference must use the approved approval:// reference form")


async def _verify_approval(
    verifier: ApprovalVerifier | None,
    *,
    reference: str,
    scope: str,
    task: dict[str, Any],
    resource_ref: str,
) -> bool:
    _validate_approval_reference(reference)
    if verifier is None or not callable(getattr(verifier, "verify", None)):
        return False
    try:
        return bool(await verifier.verify(
            reference=reference,
            scope=scope,
            tenant_id=task["tenant_id"],
            workflow_id=task["workflow_id"],
            resource_ref=resource_ref,
        ))
    except Exception:
        return False


def _release_metadata(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
) -> dict[str, Any]:
    metadata = _load_json_artifact(store, tenant_id, reference, label="release metadata")
    jsonschema.validate(metadata, _RELEASE_METADATA_SCHEMA)
    checklist_ids = [item["id"] for item in metadata["release_checklist"]]
    if len(checklist_ids) != len(set(checklist_ids)):
        raise ValueError("release checklist contains duplicate item IDs")
    if any(item["required"] and item["status"] != "passed" for item in metadata["release_checklist"]):
        raise ValueError("a required release checklist item has not passed")
    return metadata


def _rollback_plan(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
) -> dict[str, Any]:
    plan = _load_json_artifact(store, tenant_id, reference, label="rollback plan")
    jsonschema.validate(plan, _ROLLBACK_PLAN_SCHEMA)
    return plan


def _verify_build_and_test_evidence(
    store: ContentAddressedArtifactStore,
    task: dict[str, Any],
    *,
    verifier: EvidenceVerifier,
) -> dict[str, Any]:
    task_input = task["input"]
    tenant_id = task["tenant_id"]
    source_commit = task_input["commit_sha"]
    artifact_digest = task_input["artifact_digest"]

    build = _verified_evidence(
        store,
        tenant_id,
        task_input["build_evidence_ref"],
        verifier=verifier,
        expected_producer=BUILD_AGENT,
        expected_type="rusa.devops.build.attestation",
    )
    build_metadata = build.get("build_metadata")
    if not isinstance(build_metadata, dict):
        raise ValueError("Build evidence is missing provenance metadata")
    if (
        build.get("tenant_id") != tenant_id
        or build.get("workflow_id") != task["workflow_id"]
        or build.get("source_commit") != source_commit
        or build.get("artifact_digest") != artifact_digest
        or build.get("agent_id") != BUILD_AGENT
        or build_metadata.get("reproducibility_verified") is not True
    ):
        raise ValueError("Build evidence does not match the release tenant, workflow, commit, or artifact")
    artifact_ref = build.get("artifact_ref")
    if not isinstance(artifact_ref, str):
        raise ValueError("Build evidence is missing its immutable artifact reference")

    sbom_ref = build.get("sbom_ref")
    sbom_digest = build.get("sbom_digest")
    sbom_evidence_ref = build.get("sbom_evidence_ref")
    if (
        task_input["sbom_ref"] != sbom_ref
        or not isinstance(sbom_ref, str)
        or not isinstance(sbom_digest, str)
        or not isinstance(sbom_evidence_ref, str)
    ):
        raise ValueError("Release task SBOM reference does not match Build evidence")

    sbom = _verified_evidence(
        store,
        tenant_id,
        sbom_evidence_ref,
        verifier=verifier,
        expected_producer=BUILD_AGENT,
        expected_type="rusa.devops.sbom.attestation",
    )
    for key in ("tenant_id", "workflow_id", "task_id", "source_commit", "artifact_digest", "sbom_ref", "sbom_digest"):
        if sbom.get(key) != build.get(key):
            raise ValueError("Signed SBOM evidence does not match Build evidence")
    sbom_bytes = store.get(tenant_id, sbom_ref)
    if hashlib.sha256(sbom_bytes).hexdigest() != sbom_digest:
        raise ValueError("SBOM bytes do not match the signed digest")
    _validate_sbom(sbom_bytes, build.get("sbom_format"))

    test = _verified_evidence(
        store,
        tenant_id,
        task_input["test_evidence_ref"],
        verifier=verifier,
        expected_producer=TEST_AGENT,
        expected_type="rusa.devops.test.attestation",
    )
    if (
        test.get("tenant_id") != tenant_id
        or test.get("workflow_id") != task["workflow_id"]
        or test.get("source_commit") != source_commit
        or test.get("artifact_digest") != artifact_digest
        or test.get("agent_id") != TEST_AGENT
        or test.get("status") != "passed"
        or test.get("build_evidence_ref") != task_input["build_evidence_ref"]
        or test.get("sbom_evidence_ref") != sbom_evidence_ref
        or test.get("sbom_ref") != sbom_ref
        or test.get("sbom_digest") != sbom_digest
    ):
        raise ValueError("Independent QA evidence is not PASS for this exact build and SBOM")

    test_report_ref = test.get("test_report_ref")
    test_report_digest = test.get("test_report_digest")
    if not isinstance(test_report_ref, str) or not isinstance(test_report_digest, str):
        raise ValueError("Test evidence is missing its immutable report")
    test_report_bytes = store.get(tenant_id, test_report_ref)
    if hashlib.sha256(test_report_bytes).hexdigest() != test_report_digest:
        raise ValueError("Test report bytes do not match the signed digest")
    report = _load_json_artifact(store, tenant_id, test_report_ref, label="test report")
    if (
        report.get("status") != "passed"
        or report.get("source_commit") != source_commit
        or report.get("artifact_digest") != artifact_digest
        or report.get("build_evidence_ref") != task_input["build_evidence_ref"]
    ):
        raise ValueError("Test report does not confirm PASS for the exact Build evidence")
    return {
        "build": build,
        "test": test,
        "artifact_ref": artifact_ref,
        "sbom_ref": sbom_ref,
        "sbom_digest": sbom_digest,
        "sbom_evidence_ref": sbom_evidence_ref,
        "test_report_ref": test_report_ref,
        "test_report_digest": test_report_digest,
    }


class ReleaseAgentExecutor:
    """Verify all release gates and publish a signed immutable release candidate."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        signer: EvidenceSigner,
        verifier: EvidenceVerifier,
        approval_verifier: ApprovalVerifier | None = None,
        allowed_registry_hosts: set[str] | frozenset[str],
    ):
        if not callable(getattr(signer, "sign", None)) or signer.producer_id != RELEASE_AGENT:
            raise ValueError("Release Agent requires a signer for rusa_release_agent")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("Release Agent requires a trusted evidence verifier")
        self.artifact_store = artifact_store
        self.signer = signer
        self.verifier = verifier
        self.approval_verifier = approval_verifier
        self.allowed_registry_hosts = _validate_registry_hosts(allowed_registry_hosts)

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None,
    ) -> SpecialistOutcome:
        if authenticated_principal != RELEASE_AGENT:
            raise ExecutionLeaseError("Release Agent requires its authenticated workload identity")
        _validate_release_deploy_task(
            task,
            principal=authenticated_principal,
            stage="release",
            required_tools=frozenset(
                {"artifact.read", "evidence.read", "release.prepare", "manifest.write", "sbom.read", "evidence.write"}
            ),
            required_write_scopes=frozenset({"release/", "manifests/", "evidence/"}),
        )
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        source_commit = task["input"]["commit_sha"]
        artifact_digest = task["input"]["artifact_digest"]
        try:
            evidence = _verify_build_and_test_evidence(
                self.artifact_store, task, verifier=self.verifier
            )
            _validate_oci_reference(
                evidence["artifact_ref"], artifact_digest, self.allowed_registry_hosts
            )
            metadata = _release_metadata(
                self.artifact_store, task["tenant_id"], task["input"]["release_metadata_ref"]
            )
            if metadata["service"] != task["input"]["service"]:
                raise ValueError("release metadata service does not match the task")
            if metadata["migration_notes"] and not metadata["migration_compatible"]:
                raise ValueError("release contains migration changes without a compatibility pass")
            rollback_plan = _rollback_plan(
                self.artifact_store, task["tenant_id"], task["input"]["rollback_plan_ref"]
            )
            if rollback_plan["service"] != task["input"]["service"]:
                raise ValueError("rollback plan service does not match the release service")
            if not self.allowed_registry_hosts:
                raise ValueError("release registry policy is not configured")
        except (ArtifactStoreError, UnicodeDecodeError, ValueError, jsonschema.ValidationError):
            return _failure(
                task,
                started_at=started,
                code="release.gate_verification_failed",
                message="Release Agent could not verify exact Build, SBOM, independent QA, checklist, migration, or rollback evidence.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )

        constraints = task["constraints"]
        approval_required = constraints["approval_required"]
        approval_ref = constraints.get("approval_ref")
        if approval_required and "release" not in constraints.get("approval_scope", []):
            return _failure(
                task,
                started_at=started,
                code="release.approval_scope_missing",
                message="Release approval is required but the task does not carry the release approval scope.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        if approval_ref is not None:
            try:
                approved = await _verify_approval(
                    self.approval_verifier,
                    reference=approval_ref,
                    scope="release",
                    task=task,
                    resource_ref=task["input"]["release_metadata_ref"],
                )
            except Exception:
                approved = False
            if not approved:
                return _failure(
                    task,
                    started_at=started,
                    code="release.approval_invalid",
                    message="The supplied release approval could not be verified for this tenant and release metadata.",
                    status="blocked",
                    failure_category="",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )
        elif not approval_required:
            approved = True
        else:
            approved = False
        approval_status = "approved" if approved and approval_ref else "not_required" if approved else "pending_approval"

        _checkpoint(checkpoint)
        rollback_plan_ref = task["input"]["rollback_plan_ref"]
        manifest = {
            "schema_version": 1,
            "type": "rusa.devops.release-candidate",
            "tenant_id": task["tenant_id"],
            "workflow_id": task["workflow_id"],
            "task_id": task["task_id"],
            "agent_id": RELEASE_AGENT,
            "repository": evidence["build"]["repository"],
            "service": metadata["service"],
            "version": metadata["version"],
            "source_commit": source_commit,
            "artifact_ref": evidence["artifact_ref"],
            "artifact_digest": artifact_digest,
            "build_evidence_ref": task["input"]["build_evidence_ref"],
            "test_evidence_ref": task["input"]["test_evidence_ref"],
            "test_report_ref": evidence["test_report_ref"],
            "test_report_digest": evidence["test_report_digest"],
            "sbom_evidence_ref": evidence["sbom_evidence_ref"],
            "sbom_ref": evidence["sbom_ref"],
            "sbom_digest": evidence["sbom_digest"],
            "release_metadata_ref": task["input"]["release_metadata_ref"],
            "changelog": metadata["changelog"],
            "deployment_notes": metadata["deployment_notes"],
            "migration_notes": metadata["migration_notes"],
            "migration_compatible": metadata["migration_compatible"],
            "known_issues": metadata["known_issues"],
            "rollback_notes": metadata["rollback_notes"],
            "rollback_plan_ref": rollback_plan_ref,
            "release_checklist": metadata["release_checklist"],
            "approval_status": approval_status,
            "approval_ref": approval_ref if approved else None,
            "created_at": _timestamp(),
            "policy_version": task["audit"]["policy_version"],
        }
        manifest_ref = self.artifact_store.put_json(
            task["tenant_id"], manifest, namespace="artifact"
        )
        manifest_digest = hashlib.sha256(
            self.artifact_store.get(task["tenant_id"], manifest_ref)
        ).hexdigest()
        checklist_lines = [
            f"- [{'x' if item['status'] == 'passed' else ' '}] {item['id']} ({item['status']})"
            for item in metadata["release_checklist"]
        ]
        readiness_report = (
            f"# Release readiness: {metadata['service']} {metadata['version']}\n\n"
            f"- Source commit: `{source_commit}`\n"
            f"- Artifact digest: `{artifact_digest}`\n"
            "- Build provenance: verified\n"
            "- SBOM and checksum: verified\n"
            "- Independent QA: passed\n"
            f"- Migration compatibility: {'passed' if metadata['migration_compatible'] else 'not applicable'}\n"
            f"- Approval: {'approved' if approved else 'pending'}\n\n"
            "## Release checklist\n\n"
            + "\n".join(checklist_lines)
            + "\n\n## Known issues\n\n"
            + ("\n".join(f"- {item}" for item in metadata["known_issues"]) or "- None recorded")
            + "\n"
        ).encode("utf-8")
        report_ref = self.artifact_store.put(
            task["tenant_id"], readiness_report, namespace="artifact"
        )
        release_payload = {
            "schema_version": 1,
            "type": "rusa.devops.release.attestation",
            "tenant_id": task["tenant_id"],
            "workflow_id": task["workflow_id"],
            "task_id": task["task_id"],
            "attempt": task["attempt"],
            "agent_id": RELEASE_AGENT,
            "release_manifest_ref": manifest_ref,
            "release_manifest_digest": manifest_digest,
            "release_version": metadata["version"],
            "service": metadata["service"],
            "source_commit": source_commit,
            "artifact_ref": evidence["artifact_ref"],
            "artifact_digest": artifact_digest,
            "build_evidence_ref": task["input"]["build_evidence_ref"],
            "test_evidence_ref": task["input"]["test_evidence_ref"],
            "sbom_evidence_ref": evidence["sbom_evidence_ref"],
            "sbom_ref": evidence["sbom_ref"],
            "sbom_digest": evidence["sbom_digest"],
            "rollback_plan_ref": rollback_plan_ref,
            "approval_status": manifest["approval_status"],
            "approval_ref": manifest["approval_ref"],
            "created_at": _timestamp(),
            "policy_version": task["audit"]["policy_version"],
        }
        release_evidence_ref = self.artifact_store.put_json(
            task["tenant_id"],
            _signed_evidence(
                payload=release_payload,
                signer=self.signer,
                expected_producer=RELEASE_AGENT,
            ),
            namespace="evidence",
        )
        _checkpoint(checkpoint)
        if not approved:
            request_ref = _approval_request_ref(task, "release")
            result = _result(
                task,
                status="needs_approval",
                summary="Release candidate and readiness report are prepared; publication is waiting for the required human release approval.",
                artifact_refs=[manifest_ref, report_ref, rollback_plan_ref],
                evidence_refs=[release_evidence_ref],
                findings=[],
                started_at=started,
                approval_ref=request_ref,
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
            return SpecialistOutcome(result=result, failure_category="", side_effect="APPLIED")

        result = _result(
            task,
            status="passed",
            summary="All release gates passed; an immutable, signed release candidate is ready for staging deployment.",
            artifact_refs=[manifest_ref, report_ref, rollback_plan_ref],
            evidence_refs=[release_evidence_ref],
            findings=[],
            started_at=started,
            handoff={"next_agent": DEPLOY_AGENT, "event": "release.approved"},
            approval_ref=approval_ref,
            source_commit=source_commit,
            artifact_digest=artifact_digest,
        )
        return SpecialistOutcome(result=result)


class DeployAgentExecutor:
    """Deploy an approved release candidate to one configured staging target."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runner: DeploymentRunner,
        signer: EvidenceSigner,
        verifier: EvidenceVerifier,
        approval_verifier: ApprovalVerifier | None,
        allowed_registry_hosts: set[str] | frozenset[str],
        staging_target_id: str,
        allowed_services: set[str] | frozenset[str],
        allowed_network_destinations: set[str] | frozenset[str],
    ):
        for method in ("deploy", "health_check", "rehearse_rollback"):
            if not callable(getattr(runner, method, None)):
                raise TypeError(f"Deploy Agent runner must implement {method}(...)")
        if not callable(getattr(signer, "sign", None)) or signer.producer_id != DEPLOY_AGENT:
            raise ValueError("Deploy Agent requires a signer for rusa_deploy_agent")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("Deploy Agent requires a trusted evidence verifier")
        if not _DEPLOYMENT_ID_RE.fullmatch(staging_target_id):
            raise ValueError("staging target ID is invalid")
        self.artifact_store = artifact_store
        self.runner = runner
        self.signer = signer
        self.verifier = verifier
        self.approval_verifier = approval_verifier
        self.allowed_registry_hosts = frozenset(
            host.strip().lower() for host in allowed_registry_hosts if isinstance(host, str)
        )
        self.staging_target_id = staging_target_id
        self.allowed_services = frozenset(allowed_services)
        self.allowed_network_destinations = frozenset(
            destination.strip().lower() for destination in allowed_network_destinations
        )
        if not self.allowed_registry_hosts or not self.allowed_services or not self.allowed_network_destinations:
            raise ValueError("Deploy Agent requires registry, service, and network allowlists")

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
        _validate_release_deploy_task(
            task,
            principal=authenticated_principal,
            stage="deploy",
            required_tools=frozenset(
                {"artifact.read", "release.read", "deployment.execute", "health.read", "runbook.read", "evidence.write"}
            ),
            required_write_scopes=frozenset({"deployments/", "evidence/"}),
        )
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        task_input = task["input"]
        source_commit = task_input["commit_sha"]
        artifact_digest = task_input["artifact_digest"]
        service = task_input["service"]
        if task_input["environment"] != "staging":
            return _failure(
                task,
                started_at=started,
                code="deploy.environment_not_allowed",
                message="This Deploy Agent executor is restricted to the configured staging environment; production deployment is not implemented in Phase 4D.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        if not _SERVICE_RE.fullmatch(service) or service not in self.allowed_services:
            return _failure(
                task,
                started_at=started,
                code="deploy.service_not_allowed",
                message="The requested service is not in the host-configured staging deployment allowlist.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        constraints = task["constraints"]
        destinations = constraints.get("allowed_network_destinations", [])
        if (
            not constraints["network_access"]
            or not destinations
            or not {item.strip().lower() for item in destinations}.issubset(self.allowed_network_destinations)
        ):
            return _failure(
                task,
                started_at=started,
                code="deploy.network_scope_invalid",
                message="Deployment network access must be limited to host-approved staging destinations.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        try:
            release_payload = _verified_evidence(
                self.artifact_store,
                task["tenant_id"],
                task_input["release_evidence_ref"],
                verifier=self.verifier,
                expected_producer=RELEASE_AGENT,
                expected_type="rusa.devops.release.attestation",
            )
            manifest_ref = task_input["release_manifest_ref"]
            manifest_bytes = self.artifact_store.get(task["tenant_id"], manifest_ref)
            manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
            manifest = _load_json_artifact(
                self.artifact_store, task["tenant_id"], manifest_ref, label="release manifest"
            )
            if (
                release_payload.get("tenant_id") != task["tenant_id"]
                or release_payload.get("workflow_id") != task["workflow_id"]
                or release_payload.get("release_manifest_ref") != manifest_ref
                or release_payload.get("release_manifest_digest") != manifest_digest
                or release_payload.get("source_commit") != source_commit
                or release_payload.get("artifact_digest") != artifact_digest
                or release_payload.get("approval_status") not in {"approved", "not_required"}
                or manifest.get("type") != "rusa.devops.release-candidate"
                or manifest.get("tenant_id") != task["tenant_id"]
                or manifest.get("workflow_id") != task["workflow_id"]
                or manifest.get("source_commit") != source_commit
                or manifest.get("artifact_digest") != artifact_digest
                or manifest.get("approval_status") not in {"approved", "not_required"}
                or manifest.get("approval_ref") != release_payload.get("approval_ref")
                or manifest.get("service") != service
                or release_payload.get("service") != service
            ):
                raise ValueError("release attestation and manifest do not authorize this exact staging deployment")
            artifact_ref = manifest.get("artifact_ref")
            _validate_oci_reference(artifact_ref, artifact_digest, self.allowed_registry_hosts)
            if (
                manifest.get("build_evidence_ref") != release_payload.get("build_evidence_ref")
                or manifest.get("test_evidence_ref") != release_payload.get("test_evidence_ref")
                or manifest.get("sbom_ref") != release_payload.get("sbom_ref")
                or manifest.get("sbom_digest") != release_payload.get("sbom_digest")
                or manifest.get("rollback_plan_ref") != release_payload.get("rollback_plan_ref")
            ):
                raise ValueError("release manifest does not match its signed attestation")
            rollback_plan_ref = manifest.get("rollback_plan_ref")
            rollback_plan = _rollback_plan(
                self.artifact_store, task["tenant_id"], rollback_plan_ref
            )
            if (
                rollback_plan["service"] != service
                or rollback_plan["target_id"] != self.staging_target_id
            ):
                raise ValueError("rollback plan does not match the configured staging target and service")
            rollback_approved = await _verify_approval(
                self.approval_verifier,
                reference=rollback_plan["approval_ref"],
                scope="destructive_action",
                task=task,
                resource_ref=rollback_plan_ref,
            )
            if not rollback_approved:
                raise ValueError("rollback procedure has no verified pre-approval")
        except (ArtifactStoreError, UnicodeDecodeError, ValueError, TypeError, jsonschema.ValidationError):
            return _failure(
                task,
                started_at=started,
                code="deploy.release_evidence_invalid",
                message="Deploy Agent could not verify the signed release candidate, exact artifact, approved rollback plan, or staging target.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )

        approval_required = constraints["approval_required"]
        approval_ref = constraints.get("approval_ref")
        if approval_required and "deploy" not in constraints.get("approval_scope", []):
            return _failure(
                task,
                started_at=started,
                code="deploy.approval_scope_missing",
                message="Deployment approval is required but the task does not carry the deploy approval scope.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        if approval_required and approval_ref is None:
            return SpecialistOutcome(
                result=_result(
                    task,
                    status="needs_approval",
                    summary="Staging deployment is prepared but waiting for the required deployment approval.",
                    artifact_refs=[],
                    evidence_refs=[],
                    findings=[],
                    started_at=started,
                    approval_ref=_approval_request_ref(task, "deploy"),
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                ),
                failure_category="",
                side_effect="NONE",
            )
        if approval_ref is not None and not await _verify_approval(
            self.approval_verifier,
            reference=approval_ref,
            scope="deploy",
            task=task,
            resource_ref=task_input["release_manifest_ref"],
        ):
            return _failure(
                task,
                started_at=started,
                code="deploy.approval_invalid",
                message="The supplied deployment approval could not be verified for this release manifest and staging target.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )

        timeout = _timeout_seconds(task)
        deadline = time.monotonic() + timeout
        deployed: DeploymentExecutionResult | None = None
        health: HealthCheckResult | None = None
        rollback: RollbackRehearsalResult | None = None
        try:
            _checkpoint(checkpoint)
            deployed = await asyncio.wait_for(
                self.runner.deploy(
                    target_id=self.staging_target_id,
                    service=service,
                    environment="staging",
                    release_manifest_ref=manifest_ref,
                    artifact_ref=artifact_ref,
                    artifact_digest=artifact_digest,
                    timeout_seconds=max(0.1, deadline - time.monotonic()),
                    checkpoint=checkpoint,
                ),
                timeout=max(0.1, deadline - time.monotonic()),
            )
            if not isinstance(deployed, DeploymentExecutionResult):
                raise ValueError("deployment adapter returned an unsupported result")
            if (
                not _DEPLOYMENT_ID_RE.fullmatch(deployed.deployment_id)
                or deployed.target_id != self.staging_target_id
                or deployed.service != service
                or deployed.environment != "staging"
                or deployed.release_manifest_ref != manifest_ref
                or deployed.artifact_digest != artifact_digest
            ):
                raise ValueError("deployment adapter did not apply the exact approved staging release")
            deploy_started, deploy_completed = _validate_time_window(
                deployed.started_at, deployed.completed_at
            )
            _checkpoint(checkpoint)
            health = await asyncio.wait_for(
                self.runner.health_check(
                    target_id=self.staging_target_id,
                    service=service,
                    deployment_id=deployed.deployment_id,
                    artifact_digest=artifact_digest,
                    timeout_seconds=max(0.1, deadline - time.monotonic()),
                    checkpoint=checkpoint,
                ),
                timeout=max(0.1, deadline - time.monotonic()),
            )
            if not isinstance(health, HealthCheckResult) or health.deployment_id != deployed.deployment_id:
                raise ValueError("health adapter returned a result for a different deployment")
            health_checked_at, _ = _validate_time_window(health.checked_at, health.checked_at)
            names = [check.name for check in health.checks]
            if (
                not health.checks
                or len(names) != len(set(names))
                or any(not isinstance(check, HealthProbeResult) or not check.name or not isinstance(check.passed, bool) for check in health.checks)
            ):
                raise ValueError("health check evidence is empty, duplicated, or malformed")
            _checkpoint(checkpoint)
            rollback = await asyncio.wait_for(
                self.runner.rehearse_rollback(
                    target_id=self.staging_target_id,
                    service=service,
                    deployment_id=deployed.deployment_id,
                    rollback_plan_ref=rollback_plan_ref,
                    timeout_seconds=max(0.1, deadline - time.monotonic()),
                    checkpoint=checkpoint,
                ),
                timeout=max(0.1, deadline - time.monotonic()),
            )
            if (
                not isinstance(rollback, RollbackRehearsalResult)
                or rollback.deployment_id != deployed.deployment_id
                or rollback.rollback_plan_ref != rollback_plan_ref
            ):
                raise ValueError("rollback rehearsal evidence does not match the deployed release")
            rollback_completed_at, _ = _validate_time_window(rollback.completed_at, rollback.completed_at)
            if rollback.previous_artifact_digest is not None and not _SHA256_RE.fullmatch(
                rollback.previous_artifact_digest
            ):
                raise ValueError("rollback rehearsal previous artifact digest is invalid")
            _checkpoint(checkpoint)
        except ExecutionLeaseError:
            raise
        except asyncio.TimeoutError:
            return _failure(
                task,
                started_at=started,
                code="deploy.execution_timeout",
                message="Staging deployment or post-deployment verification exceeded its deadline; reconcile the staging target before retrying.",
                failure_category="AMBIGUOUS",
                side_effect="UNKNOWN" if deployed is None else "APPLIED",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        except Exception:
            return _failure(
                task,
                started_at=started,
                code="deploy.execution_failed",
                message="Staging deployment or its health/rollback verification failed; inspect the deployment adapter record before retrying.",
                failure_category="AMBIGUOUS" if deployed is None else "PERMANENT",
                side_effect="UNKNOWN" if deployed is None else "APPLIED",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )

        assert deployed is not None and health is not None and rollback is not None
        health_passed = health.healthy and health.smoke_tests_passed and all(
            check.passed for check in health.checks
        )
        rollback_passed = rollback.passed
        passed = health_passed and rollback_passed
        findings: list[dict[str, str]] = []
        if not health_passed:
            findings.append(
                {
                    "severity": "critical",
                    "code": "deploy.health_check_failed",
                    "message": "Staging health checks or smoke tests failed; no production promotion is authorized.",
                }
            )
        if not rollback_passed:
            findings.append(
                {
                    "severity": "high",
                    "code": "deploy.rollback_rehearsal_failed",
                    "message": "The approved rollback procedure did not pass its staging rehearsal.",
                }
            )
        record = {
            "schema_version": 1,
            "type": "rusa.devops.deployment-record",
            "tenant_id": task["tenant_id"],
            "workflow_id": task["workflow_id"],
            "task_id": task["task_id"],
            "deployment_id": deployed.deployment_id,
            "target_id": self.staging_target_id,
            "service": service,
            "environment": "staging",
            "release_manifest_ref": manifest_ref,
            "artifact_ref": artifact_ref,
            "artifact_digest": artifact_digest,
            "source_commit": source_commit,
            "deployment_started_at": deploy_started,
            "deployment_completed_at": deploy_completed,
            "health_check": {
                "healthy": health.healthy,
                "smoke_tests_passed": health.smoke_tests_passed,
                "checks": [asdict(check) for check in health.checks],
                "checked_at": health_checked_at,
            },
            "rollback_rehearsal": {
                "passed": rollback.passed,
                "rollback_plan_ref": rollback_plan_ref,
                "previous_artifact_digest": rollback.previous_artifact_digest,
                "completed_at": rollback_completed_at,
            },
            "status": "passed" if passed else "failed",
            "created_at": _timestamp(),
            "policy_version": task["audit"]["policy_version"],
        }
        record_ref = self.artifact_store.put_json(
            task["tenant_id"], record, namespace="artifact"
        )
        deployment_payload = {
            "schema_version": 1,
            "type": "rusa.devops.deployment.attestation",
            "tenant_id": task["tenant_id"],
            "workflow_id": task["workflow_id"],
            "task_id": task["task_id"],
            "attempt": task["attempt"],
            "agent_id": DEPLOY_AGENT,
            "deployment_record_ref": record_ref,
            "deployment_id": deployed.deployment_id,
            "target_id": self.staging_target_id,
            "service": service,
            "environment": "staging",
            "release_manifest_ref": manifest_ref,
            "artifact_digest": artifact_digest,
            "source_commit": source_commit,
            "status": record["status"],
            "created_at": _timestamp(),
            "policy_version": task["audit"]["policy_version"],
        }
        deployment_evidence_ref = self.artifact_store.put_json(
            task["tenant_id"],
            _signed_evidence(
                payload=deployment_payload,
                signer=self.signer,
                expected_producer=DEPLOY_AGENT,
            ),
            namespace="evidence",
        )
        result = _result(
            task,
            status="passed" if passed else "failed",
            summary=(
                "The approved release was deployed to staging; health, smoke, and rollback rehearsal checks passed."
                if passed
                else "Staging deployment completed but one or more health, smoke, or rollback gates failed."
            ),
            artifact_refs=[record_ref],
            evidence_refs=[deployment_evidence_ref],
            findings=findings,
            started_at=started,
            handoff=(
                {"next_agent": OPERATE_AGENT, "event": "deploy.completed"}
                if passed
                else None
            ),
            source_commit=source_commit,
            artifact_digest=artifact_digest,
        )
        return SpecialistOutcome(
            result=result,
            failure_category="" if passed else "PERMANENT",
            side_effect="APPLIED",
        )
