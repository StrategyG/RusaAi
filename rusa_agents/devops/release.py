"""Release-candidate verification and immutable manifest preparation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .agent_registry import (
    DEPLOY_AGENT,
    RELEASE_AGENT,
    SECURITY_GUARD_AGENT,
    TEST_AGENT,
    authorize_agent_task,
)
from .artifacts import ContentAddressedArtifactStore
from .build_test import (
    EvidenceSigner,
    EvidenceVerifier,
    _SHA256_RE,
    _aware_timestamp,
    _load_json_artifact,
    _signed_evidence,
    _validate_oci_reference,
    _validate_registry_hosts,
    _validate_sbom,
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
    _timestamp,
)

_RELEASE_VERSION_RE = re.compile(
    r"^(?:v)?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?$"
)


@dataclass(frozen=True)
class ReleasePolicy:
    """Host-controlled release gates; task input cannot weaken this policy."""

    required_test_suites: frozenset[str] = frozenset({"unit", "contract"})
    require_security_evidence: bool = False
    max_critical_findings: int = 0
    max_high_findings: int = 0

    def __post_init__(self) -> None:
        allowed = {
            "unit", "integration", "regression", "contract",
            "security", "smoke", "dependency", "license",
        }
        if not self.required_test_suites or not self.required_test_suites.issubset(allowed):
            raise ValueError("Release policy contains an empty or unsupported required test suite set")
        if (
            isinstance(self.max_critical_findings, bool)
            or not isinstance(self.max_critical_findings, int)
            or self.max_critical_findings < 0
            or isinstance(self.max_high_findings, bool)
            or not isinstance(self.max_high_findings, int)
            or self.max_high_findings < 0
        ):
            raise ValueError("Release policy vulnerability thresholds must be non-negative integers")


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    tenant_id: str
    workflow_id: str
    stage: str
    source_commit: str
    artifact_digest: str
    approval_id: str
    approver_id: str
    issued_at: datetime | str
    expires_at: datetime | str


class ApprovalVerifier(Protocol):
    def verify_approval(
        self,
        approval_ref: str,
        *,
        tenant_id: str,
        workflow_id: str,
        stage: str,
        source_commit: str,
        artifact_digest: str,
    ) -> ApprovalDecision: ...


def _validate_release_task(task: dict[str, Any], *, principal: str) -> None:
    policy = authorize_agent_task(task, authenticated_principal=principal)
    if policy.stage != "release":
        raise ValueError("Release Agent is only authorized for the Release stage")
    constraints = task["constraints"]
    required_tools = {
        "artifact.read", "evidence.read", "release.prepare",
        "manifest.write", "sbom.read", "evidence.write",
    }
    if not required_tools.issubset(set(constraints["allowed_tools"])):
        raise ValueError("Release task is missing a required evidence or manifest capability")
    if constraints["network_access"] or constraints.get("allowed_network_destinations"):
        raise ValueError("Release Agent cannot receive arbitrary network access")
    if not {"release/", "manifests/", "evidence/"}.issubset(set(constraints["write_scope"])):
        raise ValueError("Release task write scope does not cover candidate and evidence outputs")
    if constraints["approval_required"] and "release" not in set(constraints.get("approval_scope", [])):
        raise ValueError("Release approval must be explicitly scoped to release")
    if not constraints["approval_required"] and constraints.get("approval_ref"):
        raise ValueError("an approval reference cannot be supplied when release approval is not required")


def _text_artifact(store, tenant_id: str, reference: str, *, label: str) -> str:
    raw = store.get(tenant_id, reference)
    if not raw or len(raw) > 200_000:
        raise ValueError(f"{label} is empty or exceeds 200 KB")
    value = raw.decode("utf-8")
    if not value.strip():
        raise ValueError(f"{label} is empty")
    return value


class ReleaseAgentExecutor:
    """Verify signed Dev evidence and prepare a signed immutable release candidate."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        signer: EvidenceSigner,
        verifier: EvidenceVerifier,
        allowed_registry_hosts: set[str] | frozenset[str],
        approval_verifier: ApprovalVerifier | None = None,
        policy: ReleasePolicy | None = None,
    ):
        if getattr(signer, "producer_id", None) != RELEASE_AGENT or not callable(getattr(signer, "sign", None)):
            raise ValueError("Release Agent signer must identify rusa_release_agent")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("Release Agent requires a trusted evidence verifier")
        self.artifact_store = artifact_store
        self.signer = signer
        self.verifier = verifier
        self.allowed_registry_hosts = _validate_registry_hosts(allowed_registry_hosts)
        self.approval_verifier = approval_verifier
        self.policy = policy or ReleasePolicy()

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
        _validate_release_task(task, principal=authenticated_principal)
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        source_commit = task["input"]["commit_sha"]
        artifact_digest = task["input"]["artifact_digest"]
        try:
            build_ref = task["input"]["build_evidence_ref"]
            test_ref = task["input"]["test_evidence_ref"]
            build = _verified_evidence(
                self.artifact_store, task["tenant_id"], build_ref,
                verifier=self.verifier, expected_producer="rusa_build_agent",
                expected_type="rusa.devops.build.attestation",
            )
            test = _verified_evidence(
                self.artifact_store, task["tenant_id"], test_ref,
                verifier=self.verifier, expected_producer=TEST_AGENT,
                expected_type="rusa.devops.test.attestation",
            )
            metadata = build.get("build_metadata")
            if not isinstance(metadata, dict) or metadata.get("reproducibility_verified") is not True:
                raise ValueError("Build evidence does not prove reproducible output")
            if any(
                build.get(key) != expected
                for key, expected in (
                    ("tenant_id", task["tenant_id"]),
                    ("workflow_id", task["workflow_id"]),
                    ("source_commit", source_commit),
                    ("artifact_digest", artifact_digest),
                )
            ):
                raise ValueError("Build evidence does not match this tenant, workflow, commit, or digest")
            artifact_ref = build.get("artifact_ref")
            _validate_oci_reference(artifact_ref, artifact_digest, self.allowed_registry_hosts)
            if any(
                test.get(key) != expected
                for key, expected in (
                    ("tenant_id", task["tenant_id"]),
                    ("workflow_id", task["workflow_id"]),
                    ("source_commit", source_commit),
                    ("artifact_digest", artifact_digest),
                    ("artifact_ref", artifact_ref),
                    ("build_evidence_ref", build_ref),
                    ("status", "passed"),
                )
            ):
                raise ValueError("Test evidence does not attest the exact Build artifact")

            sbom_ref = task["input"]["sbom_ref"]
            sbom_digest = build.get("sbom_digest")
            if (
                build.get("sbom_ref") != sbom_ref
                or not isinstance(sbom_digest, str)
                or not _SHA256_RE.fullmatch("sha256:" + sbom_digest)
                or test.get("sbom_ref") != sbom_ref
                or test.get("sbom_digest") != sbom_digest
                or test.get("sbom_evidence_ref") != build.get("sbom_evidence_ref")
            ):
                raise ValueError("Build, SBOM, and Test evidence references do not match")
            sbom = _verified_evidence(
                self.artifact_store, task["tenant_id"], build.get("sbom_evidence_ref"),
                verifier=self.verifier, expected_producer="rusa_build_agent",
                expected_type="rusa.devops.sbom.attestation",
            )
            for key in ("tenant_id", "workflow_id", "source_commit", "artifact_digest", "sbom_ref", "sbom_digest"):
                if sbom.get(key) != build.get(key):
                    raise ValueError("SBOM attestation does not match Build evidence")
            sbom_bytes = self.artifact_store.get(task["tenant_id"], sbom_ref)
            if hashlib.sha256(sbom_bytes).hexdigest() != sbom_digest:
                raise ValueError("SBOM bytes do not match the signed digest")
            _validate_sbom(sbom_bytes, build["sbom_format"])

            report_ref = test.get("test_report_ref")
            report_digest = test.get("test_report_digest")
            if not isinstance(report_ref, str) or not isinstance(report_digest, str):
                raise ValueError("Test evidence is missing its report reference or digest")
            report_bytes = self.artifact_store.get(task["tenant_id"], report_ref)
            if hashlib.sha256(report_bytes).hexdigest() != report_digest:
                raise ValueError("Test report bytes do not match the signed digest")
            report = _load_json_artifact(
                self.artifact_store, task["tenant_id"], report_ref, label="test report"
            )
            if any(
                report.get(key) != expected
                for key, expected in (
                    ("status", "passed"),
                    ("source_commit", source_commit),
                    ("artifact_digest", artifact_digest),
                )
            ):
                raise ValueError("Test report does not match the exact release artifact")
            suite_rows = report.get("suites")
            if not isinstance(suite_rows, list):
                raise ValueError("Test report has no suite results")
            suites: dict[str, dict[str, Any]] = {}
            for row in suite_rows:
                if not isinstance(row, dict) or not isinstance(row.get("suite_id"), str):
                    raise ValueError("Test report contains an invalid suite record")
                if row["suite_id"] in suites:
                    raise ValueError("Test report contains duplicate suite records")
                suites[row["suite_id"]] = row
            if not self.policy.required_test_suites.issubset(suites):
                raise ValueError("Test report is missing a host-required release gate")
            for suite_id in self.policy.required_test_suites:
                row = suites[suite_id]
                if row.get("status") != "passed" or row.get("failed") != 0 or row.get("skipped") != 0:
                    raise ValueError(f"Required test suite {suite_id} did not pass")

            changelog_ref = task["input"]["changelog_ref"]
            migration_ref = task["input"]["migration_notes_ref"]
            changelog = _text_artifact(self.artifact_store, task["tenant_id"], changelog_ref, label="changelog")
            migration_notes = _text_artifact(
                self.artifact_store, task["tenant_id"], migration_ref, label="migration notes"
            )
            known_issues_ref = task["input"].get("known_issues_ref")
            known_issues = (
                _text_artifact(self.artifact_store, task["tenant_id"], known_issues_ref, label="known issues")
                if known_issues_ref else ""
            )
            security_ref = task["input"].get("security_evidence_ref")
            security_findings: list[dict[str, Any]] = []
            if security_ref:
                security = _verified_evidence(
                    self.artifact_store, task["tenant_id"], security_ref,
                    verifier=self.verifier, expected_producer=SECURITY_GUARD_AGENT,
                    expected_type="rusa.devops.security.attestation",
                )
                if any(
                    security.get(key) != expected
                    for key, expected in (
                        ("tenant_id", task["tenant_id"]),
                        ("workflow_id", task["workflow_id"]),
                        ("source_commit", source_commit),
                        ("artifact_digest", artifact_digest),
                    )
                ):
                    raise ValueError("Security evidence does not match this release artifact")
                findings = security.get("findings")
                if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
                    raise ValueError("Security evidence findings are malformed")
                security_findings = findings
            elif self.policy.require_security_evidence:
                raise ValueError("Release policy requires signed Security Guard evidence")
            critical = sum(1 for item in security_findings if item.get("severity") == "critical")
            high = sum(1 for item in security_findings if item.get("severity") == "high")
            if critical > self.policy.max_critical_findings or high > self.policy.max_high_findings:
                raise ValueError("Security findings exceed the host-controlled release thresholds")
            release_version = task["input"]["release_version"]
            if not isinstance(release_version, str) or not _RELEASE_VERSION_RE.fullmatch(release_version):
                raise ValueError("Release version must be a valid semantic release-candidate version")
        except Exception:
            return _failure(
                task, started_at=started, code="release.gate_evidence_invalid",
                message="Release Agent could not verify every required signed gate and immutable release input.",
                status="blocked", failure_category="",
                source_commit=source_commit, artifact_digest=artifact_digest,
            )

        approval_ref = task["constraints"].get("approval_ref")
        approval_id = None
        if task["constraints"]["approval_required"]:
            if not approval_ref:
                result = _result(
                    task, status="needs_approval",
                    summary="All release gates passed; the candidate is waiting for required human approval.",
                    artifact_refs=[], evidence_refs=[],
                    findings=[{
                        "severity": "info",
                        "code": "release.approval_required",
                        "message": "A release approval must be recorded before preparing a signed candidate manifest.",
                    }],
                    started_at=started,
                    approval_ref=f"approval://pending/release/{task['task_id']}",
                    source_commit=source_commit, artifact_digest=artifact_digest,
                )
                return SpecialistOutcome(result=result)
            if self.approval_verifier is None:
                return _failure(
                    task, started_at=started, code="release.approval_verifier_unavailable",
                    message="Release approval is required but no trusted approval verifier is configured.",
                    status="blocked", failure_category="",
                    source_commit=source_commit, artifact_digest=artifact_digest,
                )
            try:
                decision = self.approval_verifier.verify_approval(
                    approval_ref, tenant_id=task["tenant_id"], workflow_id=task["workflow_id"],
                    stage="release", source_commit=source_commit, artifact_digest=artifact_digest,
                )
                if (
                    not isinstance(decision, ApprovalDecision)
                    or decision.approved is not True
                    or decision.tenant_id != task["tenant_id"]
                    or decision.workflow_id != task["workflow_id"]
                    or decision.stage != "release"
                    or decision.source_commit != source_commit
                    or decision.artifact_digest != artifact_digest
                    or not decision.approval_id or not decision.approver_id
                ):
                    raise ValueError("approval decision is not bound to this release")
                issued = datetime.fromisoformat(
                    _aware_timestamp(decision.issued_at, label="approval issued_at").replace("Z", "+00:00")
                )
                expires = datetime.fromisoformat(
                    _aware_timestamp(decision.expires_at, label="approval expires_at").replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                if issued > now or expires <= now or expires <= issued:
                    raise ValueError("release approval is expired or not yet valid")
                approval_id = decision.approval_id
            except Exception:
                return _failure(
                    task, started_at=started, code="release.approval_invalid",
                    message="Release approval could not be verified for this tenant, workflow, commit, and digest.",
                    status="blocked", failure_category="",
                    source_commit=source_commit, artifact_digest=artifact_digest,
                )

        _checkpoint(checkpoint)
        created_at = _timestamp()
        manifest = {
            "schema_version": 1,
            "kind": "rusa.release.candidate",
            "status": "candidate",
            "release_version": release_version,
            "release_channel": "candidate",
            "repository": build["repository"],
            "source_commit": source_commit,
            "artifact_ref": artifact_ref,
            "artifact_digest": artifact_digest,
            "sbom_ref": sbom_ref,
            "sbom_digest": sbom_digest,
            "build_evidence_ref": task["input"]["build_evidence_ref"],
            "sbom_evidence_ref": build["sbom_evidence_ref"],
            "test_evidence_ref": task["input"]["test_evidence_ref"],
            "test_report_ref": report_ref,
            "test_report_digest": report_digest,
            "changelog_ref": changelog_ref,
            "migration_notes_ref": migration_ref,
            "known_issues_ref": known_issues_ref,
            "security_evidence_ref": security_ref,
            "approval_ref": approval_ref,
            "approval_id": approval_id,
            "created_at": created_at,
            "policy_version": task["audit"]["policy_version"],
        }
        try:
            manifest_ref = self.artifact_store.put_json(task["tenant_id"], manifest, namespace="artifact")
            report_text = (
                f"# Release readiness: {release_version}\n\n"
                f"- Status: candidate\n- Repository: {build['repository']}\n"
                f"- Source commit: {source_commit}\n- Artifact digest: {artifact_digest}\n"
                f"- Build evidence: {task['input']['build_evidence_ref']}\n"
                f"- Test evidence: {task['input']['test_evidence_ref']}\n"
                f"- SBOM: {sbom_ref} ({sbom_digest})\n"
                f"- Required suites: {', '.join(sorted(self.policy.required_test_suites))}\n"
                f"- Security findings: {critical} critical, {high} high\n"
                f"- Approval: {approval_id or 'not required'}\n\n"
                f"## Changelog\n\n{changelog[:12000]}\n\n"
                f"## Migration notes\n\n{migration_notes[:12000]}\n\n"
                f"## Known issues\n\n{known_issues[:6000] or 'None supplied'}\n"
            ).encode("utf-8")
            readiness_ref = self.artifact_store.put(task["tenant_id"], report_text, namespace="artifact")
            manifest_digest = manifest_ref.rsplit("/", 1)[-1]
            release_payload = {
                "schema_version": 1,
                "type": "rusa.devops.release.attestation",
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "agent_id": RELEASE_AGENT,
                "release_version": release_version,
                "release_manifest_ref": manifest_ref,
                "release_manifest_digest": manifest_digest,
                "readiness_report_ref": readiness_ref,
                "source_commit": source_commit,
                "artifact_digest": artifact_digest,
                "build_evidence_ref": task["input"]["build_evidence_ref"],
                "test_evidence_ref": task["input"]["test_evidence_ref"],
                "sbom_ref": sbom_ref,
                "sbom_digest": sbom_digest,
                "approval_ref": approval_ref,
                "approval_id": approval_id,
                "created_at": created_at,
                "policy_version": task["audit"]["policy_version"],
            }
            evidence_ref = self.artifact_store.put_json(
                task["tenant_id"],
                _signed_evidence(
                    payload=release_payload, signer=self.signer, expected_producer=RELEASE_AGENT
                ),
                namespace="evidence",
            )
            _checkpoint(checkpoint)
        except ExecutionLeaseError:
            raise
        except Exception:
            return _failure(
                task, started_at=started, code="release.manifest_write_failed",
                message="Release candidate manifest or signed readiness evidence could not be stored.",
                failure_category="AMBIGUOUS", side_effect="UNKNOWN",
                source_commit=source_commit, artifact_digest=artifact_digest,
            )

        result = _result(
            task, status="passed",
            summary="All release gates passed and a signed immutable release candidate was prepared; no official publication or deployment was performed.",
            artifact_refs=[manifest_ref, readiness_ref],
            evidence_refs=[evidence_ref], findings=[], started_at=started,
            handoff={"next_agent": DEPLOY_AGENT, "event": "release.ready"},
            source_commit=source_commit, artifact_digest=artifact_digest,
            approval_ref=approval_ref,
        )
        return SpecialistOutcome(result=result)
