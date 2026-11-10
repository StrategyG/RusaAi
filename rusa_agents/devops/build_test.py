"""Claim-bound reproducible Build and independent QA specialists.

Build and Test runners are injected sandbox adapters. This module never runs
task-provided shell commands, Dockerfiles, or arbitrary network requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import jsonschema

from .agent_registry import (
    BUILD_AGENT,
    RELEASE_AGENT,
    TEST_AGENT,
    authorize_agent_task,
)
from .artifacts import ArtifactStoreError, ContentAddressedArtifactStore
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


class EvidenceSigner(Protocol):
    """Ed25519 detached-JWS signer supplied by the workload host."""

    producer_id: str
    kid: str

    def sign(self, payload: bytes) -> str: ...


class EvidenceVerifier(Protocol):
    """Trusted-key verifier supplied by the workload host."""

    def verify(self, payload: bytes, detached_jws: str) -> str: ...


@dataclass(frozen=True)
class BuildExecutionResult:
    """Confirmed output from an isolated builder service."""

    artifact_ref: str
    artifact_digest: str
    rebuild_digest: str
    sbom_bytes: bytes
    sbom_format: str
    builder: str
    builder_version: str
    platform: str
    started_at: datetime | str
    completed_at: datetime | str


@dataclass(frozen=True)
class TestSuiteResult:
    suite_id: str
    status: str
    passed: int
    failed: int
    skipped: int
    duration_seconds: float


@dataclass(frozen=True)
class TestExecutionResult:
    """Result returned by the independent, isolated test service."""

    source_commit: str
    artifact_digest: str
    suite_results: tuple[TestSuiteResult, ...]
    runner: str
    runner_version: str
    environment: str
    started_at: datetime | str
    completed_at: datetime | str


class BuildRunner(Protocol):
    async def build(
        self,
        *,
        repository: str,
        commit_sha: str,
        build_config: dict[str, Any],
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> BuildExecutionResult: ...


class TestRunner(Protocol):
    async def run(
        self,
        *,
        repository: str,
        source_commit: str,
        artifact_ref: str,
        artifact_digest: str,
        test_plan: dict[str, Any],
        acceptance_criteria: str,
        timeout_seconds: float,
        checkpoint: Callable[[], bool] | None,
    ) -> TestExecutionResult: ...


_BUILD_CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "builder",
        "recipe_id",
        "platforms",
        "network_access",
        "reproducibility",
        "sbom_format",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "builder": {"enum": ["buildkit"]},
        "recipe_id": {"enum": ["container", "python-wheel", "node-package"]},
        "platforms": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"enum": ["linux/amd64", "linux/arm64"]},
        },
        "network_access": {"const": False},
        "reproducibility": {"const": "two_clean_builds"},
        "sbom_format": {"enum": ["spdx-json", "cyclonedx-json"]},
        "target": {"type": "string", "minLength": 1, "maxLength": 128},
    },
}

_TEST_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "network_access", "suites"],
    "properties": {
        "schema_version": {"const": 1},
        "network_access": {"const": False},
        "suites": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "required"],
                "properties": {
                    "id": {
                        "enum": [
                            "unit",
                            "integration",
                            "regression",
                            "contract",
                            "security",
                            "smoke",
                            "dependency",
                            "license",
                        ]
                    },
                    "required": {"type": "boolean"},
                },
            },
        },
    },
}

_SHA256_RE = re.compile(r"^sha256:([a-f0-9]{64})$")
_OCI_DIGEST_RE = re.compile(r"@sha256:[a-f0-9]{64}$")


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_artifact(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
    *,
    label: str,
) -> dict[str, Any]:
    raw = store.get(tenant_id, reference)
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} artifact is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} artifact must contain a JSON object")
    return payload


def _aware_timestamp(value: datetime | str, *, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} timestamp is invalid") from exc
    else:
        raise ValueError(f"{label} timestamp is invalid")
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_time_window(started_at: datetime | str, completed_at: datetime | str) -> tuple[str, str]:
    started = _aware_timestamp(started_at, label="started_at")
    completed = _aware_timestamp(completed_at, label="completed_at")
    if datetime.fromisoformat(completed.replace("Z", "+00:00")) < datetime.fromisoformat(
        started.replace("Z", "+00:00")
    ):
        raise ValueError("execution completion timestamp precedes its start")
    return started, completed


def _validate_oci_reference(
    artifact_ref: str,
    artifact_digest: str,
    allowed_registry_hosts: frozenset[str],
) -> None:
    if not isinstance(artifact_ref, str) or len(artifact_ref) > 512:
        raise ValueError("build artifact reference is invalid")
    parsed = urlsplit(artifact_ref)
    if (
        parsed.scheme != "oci"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or not _OCI_DIGEST_RE.search(parsed.path)
        or not parsed.path.endswith("@" + artifact_digest)
        or parsed.netloc.lower() not in allowed_registry_hosts
    ):
        raise ValueError("build artifact must be an immutable OCI digest in an approved registry")


def _validate_sbom(sbom_bytes: bytes, sbom_format: str) -> dict[str, Any]:
    if not isinstance(sbom_bytes, bytes) or not sbom_bytes or len(sbom_bytes) > 20_000_000:
        raise ValueError("SBOM must be non-empty JSON no larger than 20 MB")
    try:
        sbom = json.loads(sbom_bytes, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SBOM must be valid JSON without duplicate keys") from exc
    if not isinstance(sbom, dict):
        raise ValueError("SBOM must contain a JSON object")
    if sbom_format == "spdx-json":
        if not isinstance(sbom.get("spdxVersion"), str) or not isinstance(sbom.get("SPDXID"), str):
            raise ValueError("SPDX SBOM is missing its required identity fields")
    elif sbom_format == "cyclonedx-json":
        if (
            sbom.get("bomFormat") != "CycloneDX"
            or not isinstance(sbom.get("specVersion"), str)
            or not isinstance(sbom.get("version"), int)
            or isinstance(sbom.get("version"), bool)
        ):
            raise ValueError("CycloneDX SBOM is missing its required identity fields")
    else:
        raise ValueError("unsupported SBOM format")
    return sbom


def _validate_registry_hosts(values: set[str] | frozenset[str]) -> frozenset[str]:
    hosts = frozenset(value.strip().lower() for value in values if isinstance(value, str))
    if not hosts or any(
        not host
        or "/" in host
        or "@" in host
        or "://" in host
        or " " in host
        for host in hosts
    ):
        raise ValueError("at least one exact OCI registry host must be configured")
    return hosts


def _validate_specialist_task(
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
    if constraints["network_access"] or constraints.get("allowed_network_destinations"):
        raise ValueError("Build and Test tasks must not receive network access")
    if constraints["approval_required"] or constraints.get("approval_scope") or constraints.get("approval_ref"):
        raise ValueError("Build and Test tasks cannot self-authorize protected operations")
    if not required_write_scopes.issubset(set(constraints["write_scope"])):
        raise ValueError("task write scope does not cover the specialist evidence paths")


def _signed_evidence(
    *,
    payload: dict[str, Any],
    signer: EvidenceSigner,
    expected_producer: str,
) -> dict[str, Any]:
    if signer.producer_id != expected_producer:
        raise ValueError("evidence signer identity does not match the specialist")
    signature = signer.sign(_canonical_json(payload))
    if not isinstance(signature, str) or not signature:
        raise ValueError("evidence signer did not return a detached signature")
    return {
        "schema_version": 1,
        "producer_id": expected_producer,
        "payload": payload,
        "signature": signature,
    }


def _verified_evidence(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
    *,
    verifier: EvidenceVerifier,
    expected_producer: str,
    expected_type: str,
) -> dict[str, Any]:
    envelope = _load_json_artifact(store, tenant_id, reference, label="signed evidence")
    if set(envelope) != {"schema_version", "producer_id", "payload", "signature"}:
        raise ValueError("signed evidence envelope has unsupported fields")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1 or envelope["producer_id"] != expected_producer:
        raise ValueError("signed evidence producer or schema version is invalid")
    payload = envelope["payload"]
    signature = envelope["signature"]
    if not isinstance(payload, dict) or not isinstance(signature, str) or not signature:
        raise ValueError("signed evidence payload or signature is invalid")
    verified_producer = verifier.verify(_canonical_json(payload), signature)
    if verified_producer != expected_producer or payload.get("agent_id") != expected_producer:
        raise ValueError("signed evidence does not verify to the expected workload identity")
    if payload.get("type") != expected_type or payload.get("schema_version") != 1:
        raise ValueError("signed evidence type or payload schema version is invalid")
    return payload


def _build_config(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
) -> dict[str, Any]:
    config = _load_json_artifact(store, tenant_id, reference, label="build config")
    jsonschema.validate(config, _BUILD_CONFIG_SCHEMA)
    return config


def _test_plan(
    store: ContentAddressedArtifactStore,
    tenant_id: str,
    reference: str,
) -> dict[str, Any]:
    plan = _load_json_artifact(store, tenant_id, reference, label="test plan")
    jsonschema.validate(plan, _TEST_PLAN_SCHEMA)
    suite_ids = [item["id"] for item in plan["suites"]]
    if len(suite_ids) != len(set(suite_ids)):
        raise ValueError("test plan contains duplicate suite IDs")
    return plan


class BuildAgentExecutor:
    """Build immutable artifacts twice and sign the artifact and SBOM provenance."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runner: BuildRunner,
        signer: EvidenceSigner,
        allowed_registry_hosts: set[str] | frozenset[str],
    ):
        if not callable(getattr(runner, "build", None)):
            raise TypeError("Build Agent runner must implement async build(...)")
        if not callable(getattr(signer, "sign", None)):
            raise TypeError("Build Agent requires a detached evidence signer")
        if getattr(signer, "producer_id", None) != BUILD_AGENT:
            raise ValueError("Build Agent signer must identify rusa_build_agent")
        self.artifact_store = artifact_store
        self.runner = runner
        self.signer = signer
        self.allowed_registry_hosts = _validate_registry_hosts(allowed_registry_hosts)

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None,
    ) -> SpecialistOutcome:
        if authenticated_principal != BUILD_AGENT:
            raise ExecutionLeaseError("Build Agent requires its authenticated workload identity")
        _validate_specialist_task(
            task,
            principal=authenticated_principal,
            stage="build",
            required_tools=frozenset(
                {"repository.read", "artifact.read", "build.execute", "artifact.write", "sbom.write", "evidence.write"}
            ),
            required_write_scopes=frozenset({"build/", "artifacts/", "sbom/", "evidence/"}),
        )
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        source_commit = task["input"]["commit_sha"]
        try:
            config = _build_config(
                self.artifact_store,
                task["tenant_id"],
                task["input"]["build_config_ref"],
            )
        except (ArtifactStoreError, ValueError, jsonschema.ValidationError):
            return _failure(
                task,
                started_at=started,
                code="build.config_invalid",
                message="Build configuration is missing, malformed, or outside the approved offline build profile.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
            )

        runner_started = False
        try:
            timeout = _timeout_seconds(task)
            runner_started = True
            execution = await asyncio.wait_for(
                self.runner.build(
                    repository=task["input"]["repository"],
                    commit_sha=source_commit,
                    build_config=config,
                    timeout_seconds=timeout,
                    checkpoint=checkpoint,
                ),
                timeout=timeout,
            )
            _checkpoint(checkpoint)
            if not isinstance(execution, BuildExecutionResult):
                raise ValueError("builder returned an unsupported result type")
            if not _SHA256_RE.fullmatch(execution.artifact_digest) or not _SHA256_RE.fullmatch(
                execution.rebuild_digest
            ):
                raise ValueError("builder did not return canonical SHA-256 digests")
            if execution.artifact_digest != execution.rebuild_digest:
                return _failure(
                    task,
                    started_at=started,
                    code="build.non_reproducible",
                    message="Two clean builds produced different artifact digests; the artifact is not eligible for QA or release.",
                    failure_category="PERMANENT",
                    side_effect="APPLIED",
                    source_commit=source_commit,
                    artifact_digest=execution.artifact_digest,
                )
            _validate_oci_reference(
                execution.artifact_ref,
                execution.artifact_digest,
                self.allowed_registry_hosts,
            )
            if (
                execution.builder != config["builder"]
                or not isinstance(execution.builder_version, str)
                or not 1 <= len(execution.builder_version) <= 128
                or execution.platform not in config["platforms"]
            ):
                raise ValueError("builder provenance does not match the approved build configuration")
            build_started, build_completed = _validate_time_window(
                execution.started_at, execution.completed_at
            )
            _validate_sbom(execution.sbom_bytes, execution.sbom_format)
            if execution.sbom_format != config["sbom_format"]:
                raise ValueError("builder returned an SBOM format different from the approved configuration")

            sbom_digest = hashlib.sha256(execution.sbom_bytes).hexdigest()
            sbom_ref = self.artifact_store.put(
                task["tenant_id"], execution.sbom_bytes, namespace="artifact"
            )
            _checkpoint(checkpoint)
            created_at = _timestamp()
            common = {
                "schema_version": 1,
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "agent_id": BUILD_AGENT,
                "repository": task["input"]["repository"],
                "source_commit": source_commit,
                "artifact_ref": execution.artifact_ref,
                "artifact_digest": execution.artifact_digest,
                "sbom_ref": sbom_ref,
                "sbom_digest": sbom_digest,
                "sbom_format": execution.sbom_format,
                "build_config_ref": task["input"]["build_config_ref"],
                "created_at": created_at,
                "policy_version": task["audit"]["policy_version"],
            }
            sbom_payload = {
                **common,
                "type": "rusa.devops.sbom.attestation",
            }
            sbom_evidence_ref = self.artifact_store.put_json(
                task["tenant_id"],
                _signed_evidence(
                    payload=sbom_payload,
                    signer=self.signer,
                    expected_producer=BUILD_AGENT,
                ),
                namespace="evidence",
            )
            build_payload = {
                **common,
                "type": "rusa.devops.build.attestation",
                "sbom_evidence_ref": sbom_evidence_ref,
                "build_metadata": {
                    "builder": execution.builder,
                    "builder_version": execution.builder_version,
                    "platform": execution.platform,
                    "started_at": build_started,
                    "completed_at": build_completed,
                    "rebuild_digest": execution.rebuild_digest,
                    "reproducibility_verified": True,
                },
            }
            build_evidence_ref = self.artifact_store.put_json(
                task["tenant_id"],
                _signed_evidence(
                    payload=build_payload,
                    signer=self.signer,
                    expected_producer=BUILD_AGENT,
                ),
                namespace="evidence",
            )
            _checkpoint(checkpoint)
            result = _result(
                task,
                status="passed",
                summary=(
                    "Two clean builds produced the same immutable OCI digest; "
                    "the SBOM and build provenance were signed by the Build Agent."
                ),
                artifact_refs=[execution.artifact_ref, sbom_ref],
                evidence_refs=[sbom_evidence_ref, build_evidence_ref],
                findings=[],
                started_at=started,
                handoff={"next_agent": TEST_AGENT, "event": "build.completed"},
                source_commit=source_commit,
                artifact_digest=execution.artifact_digest,
            )
            return SpecialistOutcome(result=result)
        except ExecutionLeaseError:
            raise
        except asyncio.TimeoutError:
            return _failure(
                task,
                started_at=started,
                code="build.execution_ambiguous",
                message="The isolated build exceeded its deadline; reconcile the staging artifact registry before retrying.",
                failure_category="AMBIGUOUS",
                side_effect="UNKNOWN" if runner_started else "NONE",
                source_commit=source_commit,
            )
        except Exception:
            return _failure(
                task,
                started_at=started,
                code="build.execution_failed",
                message="The Build Agent could not produce verified reproducible build and SBOM evidence.",
                failure_category="AMBIGUOUS" if runner_started else "PERMANENT",
                side_effect="UNKNOWN" if runner_started else "NONE",
                source_commit=source_commit,
            )


class TestAgentExecutor:
    """Independently test the exact Build Agent artifact and sign QA evidence."""

    def __init__(
        self,
        *,
        artifact_store: ContentAddressedArtifactStore,
        runner: TestRunner,
        signer: EvidenceSigner,
        verifier: EvidenceVerifier,
        allowed_registry_hosts: set[str] | frozenset[str],
    ):
        if not callable(getattr(runner, "run", None)):
            raise TypeError("Test Agent runner must implement async run(...)")
        if not callable(getattr(signer, "sign", None)):
            raise TypeError("Test Agent requires a detached evidence signer")
        if getattr(signer, "producer_id", None) != TEST_AGENT:
            raise ValueError("Test Agent signer must identify rusa_test_agent")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("Test Agent requires a trusted evidence verifier")
        self.artifact_store = artifact_store
        self.runner = runner
        self.signer = signer
        self.verifier = verifier
        self.allowed_registry_hosts = _validate_registry_hosts(allowed_registry_hosts)

    async def execute(
        self,
        task: dict[str, Any],
        *,
        authenticated_principal: str,
        lease: ExecutionLease,
        checkpoint: Callable[[], bool] | None,
    ) -> SpecialistOutcome:
        if authenticated_principal != TEST_AGENT:
            raise ExecutionLeaseError("Test Agent requires its authenticated workload identity")
        _validate_specialist_task(
            task,
            principal=authenticated_principal,
            stage="test",
            required_tools=frozenset(
                {"repository.read", "artifact.read", "test.execute", "evidence.write", "metrics.read"}
            ),
            required_write_scopes=frozenset({"test-reports/", "evidence/"}),
        )
        lease.validate(task, authenticated_principal)
        _checkpoint(checkpoint)
        started = _now()
        source_commit = task["input"]["commit_sha"]
        artifact_digest = task["input"]["artifact_digest"]
        try:
            build_payload = _verified_evidence(
                self.artifact_store,
                task["tenant_id"],
                task["input"]["build_evidence_ref"],
                verifier=self.verifier,
                expected_producer=BUILD_AGENT,
                expected_type="rusa.devops.build.attestation",
            )
            build_metadata = build_payload.get("build_metadata")
            if not isinstance(build_metadata, dict):
                raise ValueError("Build evidence is missing build provenance metadata")
            if (
                build_payload.get("tenant_id") != task["tenant_id"]
                or build_payload.get("workflow_id") != task["workflow_id"]
                or build_payload.get("source_commit") != source_commit
                or build_payload.get("artifact_digest") != artifact_digest
                or build_payload.get("agent_id") != BUILD_AGENT
                or build_metadata.get("reproducibility_verified") is not True
            ):
                raise ValueError("Build evidence does not match this tenant, workflow, commit, or artifact")
            artifact_ref = build_payload.get("artifact_ref")
            _validate_oci_reference(artifact_ref, artifact_digest, self.allowed_registry_hosts)
            repository = build_payload.get("repository")
            if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                raise ValueError("Build evidence repository identity is invalid")
            sbom_ref = build_payload.get("sbom_ref")
            sbom_digest = build_payload.get("sbom_digest")
            if not isinstance(sbom_ref, str) or not isinstance(sbom_digest, str):
                raise ValueError("Build evidence is missing its SBOM reference or digest")
            sbom_payload = _verified_evidence(
                self.artifact_store,
                task["tenant_id"],
                build_payload.get("sbom_evidence_ref"),
                verifier=self.verifier,
                expected_producer=BUILD_AGENT,
                expected_type="rusa.devops.sbom.attestation",
            )
            for key in (
                "tenant_id",
                "workflow_id",
                "task_id",
                "source_commit",
                "artifact_digest",
                "sbom_ref",
                "sbom_digest",
                "sbom_format",
            ):
                if sbom_payload.get(key) != build_payload.get(key):
                    raise ValueError("Signed SBOM attestation does not match Build evidence")
            sbom_bytes = self.artifact_store.get(task["tenant_id"], sbom_ref)
            if hashlib.sha256(sbom_bytes).hexdigest() != sbom_digest:
                raise ValueError("SBOM bytes do not match the signed SBOM digest")
            _validate_sbom(sbom_bytes, build_payload["sbom_format"])
            test_plan = _test_plan(
                self.artifact_store,
                task["tenant_id"],
                task["input"]["test_plan_ref"],
            )
            acceptance_bytes = self.artifact_store.get(
                task["tenant_id"], task["input"]["acceptance_criteria_ref"]
            )
            if not acceptance_bytes or len(acceptance_bytes) > 200_000:
                raise ValueError("acceptance criteria artifact is empty or exceeds 200 KB")
            acceptance_criteria = acceptance_bytes.decode("utf-8")
            if not acceptance_criteria.strip():
                raise ValueError("acceptance criteria artifact is empty")
        except (ArtifactStoreError, UnicodeDecodeError, ValueError, jsonschema.ValidationError):
            return _failure(
                task,
                started_at=started,
                code="test.build_evidence_invalid",
                message="Test Agent could not verify the signed Build/SBOM evidence and exact immutable inputs.",
                status="blocked",
                failure_category="",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )

        try:
            timeout = _timeout_seconds(task)
            execution = await asyncio.wait_for(
                self.runner.run(
                    repository=repository,
                    source_commit=source_commit,
                    artifact_ref=artifact_ref,
                    artifact_digest=artifact_digest,
                    test_plan=test_plan,
                    acceptance_criteria=acceptance_criteria,
                    timeout_seconds=timeout,
                    checkpoint=checkpoint,
                ),
                timeout=timeout,
            )
            _checkpoint(checkpoint)
            if not isinstance(execution, TestExecutionResult):
                raise ValueError("test runner returned an unsupported result type")
            if (
                execution.source_commit != source_commit
                or execution.artifact_digest != artifact_digest
            ):
                return _failure(
                    task,
                    started_at=started,
                    code="test.execution_target_mismatch",
                    message="The Test Agent runner did not test the exact source commit and artifact digest.",
                    status="blocked",
                    failure_category="",
                    source_commit=source_commit,
                    artifact_digest=artifact_digest,
                )
            if (
                not isinstance(execution.runner, str)
                or not 1 <= len(execution.runner) <= 128
                or not isinstance(execution.runner_version, str)
                or not 1 <= len(execution.runner_version) <= 128
                or execution.environment not in {"sandbox", "staging"}
            ):
                raise ValueError("test runner provenance is invalid")
            test_started, test_completed = _validate_time_window(
                execution.started_at, execution.completed_at
            )
            requested = {item["id"]: item["required"] for item in test_plan["suites"]}
            actual: dict[str, TestSuiteResult] = {}
            for suite in execution.suite_results:
                if not isinstance(suite, TestSuiteResult) or suite.suite_id not in requested:
                    raise ValueError("test runner returned an unknown or invalid suite result")
                if suite.suite_id in actual:
                    raise ValueError("test runner returned a duplicate suite result")
                for count in (suite.passed, suite.failed, suite.skipped):
                    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                        raise ValueError("test suite counts must be non-negative integers")
                if (
                    isinstance(suite.duration_seconds, bool)
                    or not isinstance(suite.duration_seconds, (int, float))
                    or not math.isfinite(suite.duration_seconds)
                    or suite.duration_seconds < 0
                ):
                    raise ValueError("test suite duration is invalid")
                if suite.status not in {"passed", "failed", "skipped"}:
                    raise ValueError("test suite status is invalid")
                if suite.status == "passed" and (suite.passed < 1 or suite.failed != 0 or suite.skipped != 0):
                    raise ValueError("a passing suite cannot contain failed or skipped tests")
                if suite.status == "failed" and suite.failed < 1:
                    raise ValueError("a failed suite must report at least one failed test")
                if suite.status == "skipped" and (suite.passed or suite.failed or suite.skipped):
                    raise ValueError("a skipped suite cannot report executed test counts")
                actual[suite.suite_id] = suite
            if set(actual) != set(requested):
                raise ValueError("test runner did not return exactly the requested test suites")

            findings: list[dict[str, str]] = []
            for suite_id, suite in actual.items():
                if suite.status == "failed":
                    findings.append(
                        {
                            "severity": "high",
                            "code": "test.suite_failed",
                            "message": f"Required or optional test suite {suite_id} reported failures.",
                        }
                    )
                elif suite.status == "skipped" and requested[suite_id]:
                    findings.append(
                        {
                            "severity": "high",
                            "code": "test.required_suite_skipped",
                            "message": f"Required test suite {suite_id} was skipped.",
                        }
                    )
            passed = not findings
            report = {
                "schema_version": 1,
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "source_commit": source_commit,
                "artifact_digest": artifact_digest,
                "artifact_ref": artifact_ref,
                "build_evidence_ref": task["input"]["build_evidence_ref"],
                "test_plan_ref": task["input"]["test_plan_ref"],
                "acceptance_criteria_ref": task["input"]["acceptance_criteria_ref"],
                "runner": execution.runner,
                "runner_version": execution.runner_version,
                "environment": execution.environment,
                "started_at": test_started,
                "completed_at": test_completed,
                "suites": [asdict(actual[suite_id]) for suite_id in requested],
                "status": "passed" if passed else "failed",
            }
            report_ref = self.artifact_store.put_json(
                task["tenant_id"], report, namespace="evidence"
            )
            report_digest = report_ref.rsplit("/", 1)[-1]
            evidence_payload = {
                "schema_version": 1,
                "type": "rusa.devops.test.attestation",
                "tenant_id": task["tenant_id"],
                "workflow_id": task["workflow_id"],
                "task_id": task["task_id"],
                "attempt": task["attempt"],
                "agent_id": TEST_AGENT,
                "source_commit": source_commit,
                "artifact_digest": artifact_digest,
                "artifact_ref": artifact_ref,
                "build_evidence_ref": task["input"]["build_evidence_ref"],
                "sbom_evidence_ref": build_payload["sbom_evidence_ref"],
                "sbom_ref": sbom_ref,
                "sbom_digest": sbom_digest,
                "test_plan_ref": task["input"]["test_plan_ref"],
                "acceptance_criteria_ref": task["input"]["acceptance_criteria_ref"],
                "test_report_ref": report_ref,
                "test_report_digest": report_digest,
                "status": "passed" if passed else "failed",
                "created_at": _timestamp(),
                "policy_version": task["audit"]["policy_version"],
            }
            evidence_ref = self.artifact_store.put_json(
                task["tenant_id"],
                _signed_evidence(
                    payload=evidence_payload,
                    signer=self.signer,
                    expected_producer=TEST_AGENT,
                ),
                namespace="evidence",
            )
            _checkpoint(checkpoint)
            result = _result(
                task,
                status="passed" if passed else "failed",
                summary=(
                    "Independent QA passed for the exact Build Agent artifact."
                    if passed
                    else "Independent QA failed; the artifact is not eligible for release."
                ),
                artifact_refs=[report_ref],
                evidence_refs=[evidence_ref],
                findings=findings,
                started_at=started,
                handoff=(
                    {"next_agent": RELEASE_AGENT, "event": "test.completed"}
                    if passed
                    else None
                ),
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
            return SpecialistOutcome(
                result=result,
                failure_category="" if passed else "PERMANENT",
                side_effect="NONE",
            )
        except ExecutionLeaseError:
            raise
        except asyncio.TimeoutError:
            return _failure(
                task,
                started_at=started,
                code="test.execution_timeout",
                message="Independent QA exceeded its execution deadline; no passing evidence was produced.",
                failure_category="TRANSIENT",
                side_effect="NONE",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
        except Exception:
            return _failure(
                task,
                started_at=started,
                code="test.execution_failed",
                message="Independent QA could not produce a valid report for the exact Build Agent artifact.",
                failure_category="PERMANENT",
                side_effect="NONE",
                source_commit=source_commit,
                artifact_digest=artifact_digest,
            )
