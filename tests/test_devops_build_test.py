from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from rusa_agents.devops.agent_registry import AGENT_POLICIES, BUILD_AGENT, TEST_AGENT
from rusa_agents.devops.artifacts import ContentAddressedArtifactStore
from rusa_agents.devops.build_test import (
    BuildAgentExecutor,
    BuildExecutionResult,
    TestAgentExecutor,
    TestExecutionResult,
    TestSuiteResult,
)
from rusa_agents.devops.specialists import ExecutionLease

REGISTRY_HOST = "registry.example"
SOURCE_COMMIT = "a" * 40
ARTIFACT_DIGEST = "sha256:" + "b" * 64
ARTIFACT_REF = f"oci://{REGISTRY_HOST}/rusa/rusa-ai@{ARTIFACT_DIGEST}"
SBOM_BYTES = (
    b'{"spdxVersion":"SPDX-2.3","SPDXID":"SPDXRef-DOCUMENT",'
    b'"name":"rusa-ai"}'
)
BUILD_CONFIG = {
    "schema_version": 1,
    "builder": "buildkit",
    "recipe_id": "container",
    "platforms": ["linux/amd64"],
    "network_access": False,
    "reproducibility": "two_clean_builds",
    "sbom_format": "spdx-json",
}
TEST_PLAN = {
    "schema_version": 1,
    "network_access": False,
    "suites": [
        {"id": "unit", "required": True},
        {"id": "contract", "required": True},
    ],
}


class FakeSigner:
    def __init__(self, producer_id: str, key: bytes):
        self.producer_id = producer_id
        self.kid = f"{producer_id}-test-key"
        self.key = key

    def sign(self, payload: bytes) -> str:
        digest = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
        return f"{self.producer_id}.{digest}"


class FakeVerifier:
    def __init__(self, keys: dict[str, bytes]):
        self.keys = keys

    def verify(self, payload: bytes, detached_jws: str) -> str:
        producer, separator, digest = detached_jws.partition(".")
        key = self.keys.get(producer)
        if not separator or key is None:
            raise ValueError("untrusted test signature")
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            raise ValueError("test signature mismatch")
        return producer


class FakeBuildRunner:
    def __init__(self, result: BuildExecutionResult | None = None):
        self.result = result or _build_result()
        self.calls = 0
        self.arguments = None

    async def build(self, **kwargs):
        self.calls += 1
        self.arguments = kwargs
        return self.result


class FakeTestRunner:
    def __init__(self, result: TestExecutionResult | None = None):
        self.result = result or _test_result()
        self.calls = 0
        self.arguments = None

    async def run(self, **kwargs):
        self.calls += 1
        self.arguments = kwargs
        return self.result


def _build_result(
    *,
    artifact_digest: str = ARTIFACT_DIGEST,
    rebuild_digest: str = ARTIFACT_DIGEST,
) -> BuildExecutionResult:
    now = datetime.now(timezone.utc)
    return BuildExecutionResult(
        artifact_ref=f"oci://{REGISTRY_HOST}/rusa/rusa-ai@{artifact_digest}",
        artifact_digest=artifact_digest,
        rebuild_digest=rebuild_digest,
        sbom_bytes=SBOM_BYTES,
        sbom_format="spdx-json",
        builder="buildkit",
        builder_version="0.20.0",
        platform="linux/amd64",
        started_at=now - timedelta(seconds=10),
        completed_at=now,
    )


def _test_result(
    *,
    source_commit: str = SOURCE_COMMIT,
    artifact_digest: str = ARTIFACT_DIGEST,
    unit_status: str = "passed",
) -> TestExecutionResult:
    now = datetime.now(timezone.utc)
    unit = TestSuiteResult(
        suite_id="unit",
        status=unit_status,
        passed=0 if unit_status == "skipped" else 12,
        failed=1 if unit_status == "failed" else 0,
        skipped=0,
        duration_seconds=1.25,
    )
    if unit_status == "skipped":
        unit = TestSuiteResult("unit", "skipped", 0, 0, 0, 0.0)
    contract = TestSuiteResult(
        suite_id="contract",
        status="passed",
        passed=8,
        failed=0,
        skipped=0,
        duration_seconds=0.75,
    )
    return TestExecutionResult(
        source_commit=source_commit,
        artifact_digest=artifact_digest,
        suite_results=(unit, contract),
        runner="rusa-isolated-test-runner",
        runner_version="1.4.0",
        environment="sandbox",
        started_at=now - timedelta(seconds=3),
        completed_at=now,
    )


def _task(stage: str, *, tenant_id: str = "tenant-build-qa") -> dict:
    now = datetime.now(timezone.utc)
    if stage == "build":
        agent_id = BUILD_AGENT
        inputs = {
            "repository": "StrategyG/RusaAi",
            "commit_sha": SOURCE_COMMIT,
            "build_config_ref": "artifact://sha256/" + "0" * 64,
        }
        write_scope = ["build/", "artifacts/", "sbom/", "evidence/"]
    elif stage == "test":
        agent_id = TEST_AGENT
        inputs = {
            "repository": "StrategyG/RusaAi",
            "commit_sha": SOURCE_COMMIT,
            "artifact_digest": ARTIFACT_DIGEST,
            "build_evidence_ref": "evidence://sha256/" + "1" * 64,
            "test_plan_ref": "artifact://sha256/" + "2" * 64,
            "acceptance_criteria_ref": "artifact://sha256/" + "3" * 64,
        }
        write_scope = ["test-reports/", "evidence/"]
    else:
        raise ValueError("unsupported test fixture stage")
    return {
        "schema_version": 1,
        "task_id": f"task-{uuid4().hex[:12]}",
        "workflow_id": f"run-{uuid4().hex[:12]}",
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "stage": stage,
        "objective": f"Run the {stage} lifecycle specialist.",
        "input": inputs,
        "constraints": {
            "allowed_tools": sorted(AGENT_POLICIES[agent_id].allowed_tools),
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": write_scope,
            "approval_required": False,
            "approval_scope": [],
            "budget": {
                "max_cost_usd": 2.0,
                "max_tool_calls": 20,
                "max_runtime_seconds": 300,
            },
        },
        "attempt": 1,
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
        "audit": {
            "created_at": now.isoformat(),
            "requested_by": "pytest",
            "policy_version": "policy-build-qa-test-v1",
        },
    }


def _lease(task: dict, *, principal: str | None = None) -> ExecutionLease:
    return ExecutionLease(
        tenant_id=task["tenant_id"],
        run_id=task["workflow_id"],
        task_id=task["task_id"],
        attempt=task["attempt"],
        principal=principal or task["agent_id"],
        claim_id=f"claim-{uuid4().hex}",
        lease_epoch=1,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=2),
        accepted=True,
    )


def _store_build_config(store: ContentAddressedArtifactStore, task: dict) -> str:
    reference = store.put_json(task["tenant_id"], BUILD_CONFIG, namespace="artifact")
    task["input"]["build_config_ref"] = reference
    return reference


async def _run_build(
    tmp_path,
    *,
    runner: FakeBuildRunner | None = None,
    signer: FakeSigner | None = None,
):
    task = _task("build")
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    _store_build_config(store, task)
    build_signer = signer or FakeSigner(BUILD_AGENT, b"build-signing-test-key")
    build_runner = runner or FakeBuildRunner()
    executor = BuildAgentExecutor(
        artifact_store=store,
        runner=build_runner,
        signer=build_signer,
        allowed_registry_hosts={REGISTRY_HOST},
    )
    outcome = await executor.execute(
        task,
        authenticated_principal=BUILD_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )
    return task, store, outcome, build_runner, build_signer


async def _run_build_then_test(tmp_path, *, test_runner: FakeTestRunner | None = None):
    build_task, store, build_outcome, build_runner, build_signer = await _run_build(tmp_path)
    assert build_outcome.result["status"] == "passed"
    build_evidence_ref = build_outcome.result["output"]["evidence_refs"][-1]

    test_task = _task("test", tenant_id=build_task["tenant_id"])
    test_task["workflow_id"] = build_task["workflow_id"]
    test_task["input"]["build_evidence_ref"] = build_evidence_ref
    test_plan_ref = store.put_json(test_task["tenant_id"], TEST_PLAN, namespace="artifact")
    test_task["input"]["test_plan_ref"] = test_plan_ref
    acceptance_ref = store.put(
        test_task["tenant_id"],
        b"# Acceptance criteria\n\n- The built artifact satisfies AC-1.\n",
        namespace="artifact",
    )
    test_task["input"]["acceptance_criteria_ref"] = acceptance_ref
    test_signer = FakeSigner(TEST_AGENT, b"test-signing-test-key")
    verifier = FakeVerifier(
        {
            BUILD_AGENT: build_signer.key,
            TEST_AGENT: test_signer.key,
        }
    )
    runner = test_runner or FakeTestRunner()
    executor = TestAgentExecutor(
        artifact_store=store,
        runner=runner,
        signer=test_signer,
        verifier=verifier,
        allowed_registry_hosts={REGISTRY_HOST},
    )
    outcome = await executor.execute(
        test_task,
        authenticated_principal=TEST_AGENT,
        lease=_lease(test_task),
        checkpoint=lambda: True,
    )
    return test_task, store, outcome, runner, verifier


@pytest.mark.asyncio
async def test_build_agent_emits_reproducible_signed_build_and_sbom_evidence(tmp_path):
    task, store, outcome, runner, signer = await _run_build(tmp_path)

    assert outcome.result["status"] == "passed"
    assert outcome.result["audit"]["source_commit"] == SOURCE_COMMIT
    assert outcome.result["audit"]["artifact_digest"] == ARTIFACT_DIGEST
    assert outcome.result["handoff"] == {
        "next_agent": TEST_AGENT,
        "event": "build.completed",
    }
    assert len(outcome.result["output"]["evidence_refs"]) == 2
    sbom_evidence_ref, build_evidence_ref = outcome.result["output"]["evidence_refs"]
    sbom_evidence = store.get_json(task["tenant_id"], sbom_evidence_ref)
    build_evidence = store.get_json(task["tenant_id"], build_evidence_ref)
    assert sbom_evidence["producer_id"] == BUILD_AGENT
    assert build_evidence["producer_id"] == BUILD_AGENT
    assert build_evidence["payload"]["source_commit"] == SOURCE_COMMIT
    assert build_evidence["payload"]["artifact_digest"] == ARTIFACT_DIGEST
    assert build_evidence["payload"]["sbom_evidence_ref"] == sbom_evidence_ref
    assert build_evidence["payload"]["build_metadata"]["reproducibility_verified"] is True
    assert signer.producer_id == BUILD_AGENT
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_build_agent_blocks_non_reproducible_artifact_and_records_side_effect(tmp_path):
    runner = FakeBuildRunner(
        _build_result(rebuild_digest="sha256:" + "c" * 64)
    )
    _, _, outcome, _, _ = await _run_build(tmp_path, runner=runner)

    assert outcome.result["status"] == "failed"
    assert outcome.result["output"]["findings"][0]["code"] == "build.non_reproducible"
    assert outcome.failure_category == "PERMANENT"
    assert outcome.side_effect == "APPLIED"
    assert "handoff" not in outcome.result


@pytest.mark.asyncio
async def test_build_agent_rejects_network_enabled_build_profile_before_runner(tmp_path):
    task = _task("build")
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    unsafe_config = {**BUILD_CONFIG, "network_access": True}
    task["input"]["build_config_ref"] = store.put_json(
        task["tenant_id"], unsafe_config, namespace="artifact"
    )
    runner = FakeBuildRunner()
    executor = BuildAgentExecutor(
        artifact_store=store,
        runner=runner,
        signer=FakeSigner(BUILD_AGENT, b"build-signing-test-key"),
        allowed_registry_hosts={REGISTRY_HOST},
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=BUILD_AGENT,
        lease=_lease(task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "build.config_invalid"
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_test_agent_verifies_build_and_sbom_then_signs_exact_commit_qa(tmp_path):
    task, store, outcome, runner, verifier = await _run_build_then_test(tmp_path)

    assert outcome.result["status"] == "passed"
    assert outcome.result["audit"]["source_commit"] == SOURCE_COMMIT
    assert outcome.result["audit"]["artifact_digest"] == ARTIFACT_DIGEST
    assert outcome.result["handoff"] == {
        "next_agent": "rusa_release_agent",
        "event": "test.completed",
    }
    assert runner.calls == 1
    assert runner.arguments["source_commit"] == SOURCE_COMMIT
    assert runner.arguments["artifact_digest"] == ARTIFACT_DIGEST
    assert runner.arguments["artifact_ref"] == ARTIFACT_REF
    evidence_ref = outcome.result["output"]["evidence_refs"][0]
    envelope = store.get_json(task["tenant_id"], evidence_ref)
    payload = envelope["payload"]
    assert envelope["producer_id"] == TEST_AGENT
    assert verifier.verify(
        __import__("json").dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8"),
        envelope["signature"],
    ) == TEST_AGENT
    assert payload["source_commit"] == SOURCE_COMMIT
    assert payload["artifact_digest"] == ARTIFACT_DIGEST
    assert payload["status"] == "passed"


@pytest.mark.asyncio
async def test_test_agent_rejects_tampered_build_signature_without_running_tests(tmp_path):
    build_task, store, build_outcome, build_runner, build_signer = await _run_build(tmp_path)
    build_evidence_ref = build_outcome.result["output"]["evidence_refs"][-1]
    original = store.get_json(build_task["tenant_id"], build_evidence_ref)
    tampered = {**original, "signature": "rusa_build_agent.invalid"}
    tampered_ref = store.put_json(
        build_task["tenant_id"], tampered, namespace="evidence"
    )
    test_task = _task("test", tenant_id=build_task["tenant_id"])
    test_task["workflow_id"] = build_task["workflow_id"]
    test_task["input"]["build_evidence_ref"] = tampered_ref
    test_task["input"]["test_plan_ref"] = store.put_json(
        test_task["tenant_id"], TEST_PLAN, namespace="artifact"
    )
    test_task["input"]["acceptance_criteria_ref"] = store.put(
        test_task["tenant_id"], b"AC-1", namespace="artifact"
    )
    test_signer = FakeSigner(TEST_AGENT, b"test-signing-test-key")
    runner = FakeTestRunner()
    executor = TestAgentExecutor(
        artifact_store=store,
        runner=runner,
        signer=test_signer,
        verifier=FakeVerifier({BUILD_AGENT: build_signer.key}),
        allowed_registry_hosts={REGISTRY_HOST},
    )

    outcome = await executor.execute(
        test_task,
        authenticated_principal=TEST_AGENT,
        lease=_lease(test_task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "test.build_evidence_invalid"
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_test_agent_cannot_pass_when_required_suite_is_skipped(tmp_path):
    runner = FakeTestRunner(_test_result(unit_status="skipped"))
    _, _, outcome, runner, _ = await _run_build_then_test(tmp_path, test_runner=runner)

    assert outcome.result["status"] == "failed"
    assert outcome.result["output"]["findings"][0]["code"] == "test.required_suite_skipped"
    assert "handoff" not in outcome.result
    assert outcome.result["output"]["evidence_refs"]
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_test_agent_rejects_runner_output_for_a_different_artifact(tmp_path):
    runner = FakeTestRunner(
        _test_result(artifact_digest="sha256:" + "d" * 64)
    )
    _, _, outcome, runner, _ = await _run_build_then_test(tmp_path, test_runner=runner)

    assert outcome.result["status"] == "blocked"
    assert outcome.result["output"]["findings"][0]["code"] == "test.execution_target_mismatch"
    assert "handoff" not in outcome.result


@pytest.mark.asyncio
async def test_test_agent_rejects_build_evidence_from_another_tenant(tmp_path):
    build_task, store, build_outcome, build_runner, build_signer = await _run_build(tmp_path)
    build_evidence_ref = build_outcome.result["output"]["evidence_refs"][-1]
    test_task = _task("test", tenant_id="tenant-other")
    test_task["input"]["build_evidence_ref"] = build_evidence_ref
    test_task["input"]["test_plan_ref"] = store.put_json(
        test_task["tenant_id"], TEST_PLAN, namespace="artifact"
    )
    test_task["input"]["acceptance_criteria_ref"] = store.put(
        test_task["tenant_id"], b"AC-1", namespace="artifact"
    )
    test_signer = FakeSigner(TEST_AGENT, b"test-signing-test-key")
    runner = FakeTestRunner()
    executor = TestAgentExecutor(
        artifact_store=store,
        runner=runner,
        signer=test_signer,
        verifier=FakeVerifier({BUILD_AGENT: build_signer.key}),
        allowed_registry_hosts={REGISTRY_HOST},
    )

    outcome = await executor.execute(
        test_task,
        authenticated_principal=TEST_AGENT,
        lease=_lease(test_task),
        checkpoint=lambda: True,
    )

    assert outcome.result["status"] == "blocked"
    assert runner.calls == 0


def test_build_and_test_signers_are_bound_to_specialist_identity(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="Build Agent signer"):
        BuildAgentExecutor(
            artifact_store=store,
            runner=FakeBuildRunner(),
            signer=FakeSigner(TEST_AGENT, b"wrong"),
            allowed_registry_hosts={REGISTRY_HOST},
        )
