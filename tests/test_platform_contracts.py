from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from rusa_agents.contract_adapters import (
    agent_result_to_worker_result,
    coding_task_to_envelope,
    queue_status_from_legacy,
    queue_status_to_legacy,
    snapshot_to_task_record,
    task_envelope_to_coding_task,
    task_record_to_snapshot,
    worker_result_to_agent_result,
)
from rusa_agents.contracts import (
    CodingTask,
    RepositoryTarget,
    TaskRecord,
    TaskState,
    WorkerResult,
    WorkerRole,
)
from rusa_agents.platform_contracts import (
    PLATFORM_SCHEMA_MODELS,
    AICodeQueueStatusV1,
    AICodeTaskStateV1,
    AgentDomain,
    AgentHealthStatus,
    AgentManifestV1,
    AgentResultV2,
    AgentStatus,
    ArtifactReference,
    EvidenceReference,
    TaskEnvelopeV2,
    TaskOutcome,
    TaskRecordSnapshotV2,
    WorkflowEventV2,
    normalize_contract_schema,
    platform_contract_schema,
    validate_manifest_update,
)

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


def _manifest_payload() -> dict:
    return {
        "agent_id": "rusa_plan_agent",
        "domain": "software_engineering",
        "version": "1.0.0",
        "status": "validated",
        "capabilities": ["requirements.plan"],
        "input_schema_ref": "schema://rusa/plan/input.v1",
        "output_schema_ref": "schema://rusa/plan/output.v1",
        "runtime_binding": {
            "kind": "python_callable",
            "target": "rusa_agents.planning:PlanAgent",
        },
        "tool_allowlist": ["read_repository"],
        "tenant_scope": {"mode": "tenant"},
        "policy_profile": "software-engineering-default",
        "evaluation_profile": "planning-v1",
        "owner": "platform-engineering",
        "health": {"status": "unknown"},
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


def _task_payload() -> dict:
    return {
        "tenant_id": "tenant-a",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "correlation_id": "workflow-1",
        "domain": "software_engineering",
        "agent_id": "aicode.legacy_adapter",
        "agent_version": "1.0.0",
        "capability": "coding.execute",
        "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
        "created_at": NOW.isoformat(),
        "budget": {"max_execution_seconds": 3600, "max_tool_calls": 100},
        "input": {"objective": "Add a test"},
        "approval_required": True,
        "approval_ref": "approval://change-123",
    }


def _result_payload() -> dict:
    return {
        "tenant_id": "tenant-a",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "agent_id": "aicode.worker.tester",
        "agent_version": "1.0.0",
        "capability": "coding.tester",
        "status": "succeeded",
        "summary": "Tests passed",
        "started_at": NOW.isoformat(),
        "completed_at": (NOW + timedelta(seconds=2)).isoformat(),
        "output_refs": [
            {
                "uri": "artifact://runs/run-1/test-report.json",
                "sha256": "sha256:" + "a" * 64,
            }
        ],
    }


def _event_payload() -> dict:
    return {
        "tenant_id": "tenant-a",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "correlation_id": "workflow-1",
        "event_type": "task.accepted",
        "producer_id": "aicode.worker.tester",
        "producer_version": "1.0.0",
        "occurred_at": NOW.isoformat(),
        "payload": {"worker_id": "worker-1", "lease_epoch": 1, "state_version": 1},
    }


def test_agent_manifest_validates_and_rejects_duplicate_capabilities() -> None:
    manifest = AgentManifestV1.model_validate(_manifest_payload())
    assert manifest.status == AgentStatus.VALIDATED
    assert manifest.domain == AgentDomain.SOFTWARE_ENGINEERING

    payload = _manifest_payload()
    payload["capabilities"] = ["requirements.plan", "requirements.plan"]
    with pytest.raises(ValidationError, match="capabilities must be unique"):
        AgentManifestV1.model_validate(payload)


def test_agent_manifest_rejects_unknown_fields_and_invalid_versions() -> None:
    payload = _manifest_payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentManifestV1.model_validate(payload)

    payload = _manifest_payload()
    payload["version"] = "latest"
    with pytest.raises(ValidationError):
        AgentManifestV1.model_validate(payload)


def test_agent_manifest_requires_explicit_runtime_and_policy_profiles() -> None:
    payload = _manifest_payload()
    del payload["runtime_binding"]
    with pytest.raises(ValidationError):
        AgentManifestV1.model_validate(payload)


def test_task_envelope_requires_timezone_aware_future_deadline() -> None:
    envelope = TaskEnvelopeV2.model_validate(_task_payload())
    assert envelope.tenant_id == "tenant-a"
    assert envelope.budget.max_tool_calls == 100

    payload = _task_payload()
    payload["deadline_at"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValidationError, match="deadline_at must be later"):
        TaskEnvelopeV2.model_validate(payload)

    payload = _task_payload()
    payload["deadline_at"] = (NOW + timedelta(hours=1)).replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timestamp must include a timezone"):
        TaskEnvelopeV2.model_validate(payload)


def test_task_envelope_rejects_credentials_in_durable_payloads() -> None:
    payload = _task_payload()
    payload["input"] = {"nested": {"api_key": "do-not-persist"}}
    with pytest.raises(ValidationError, match="credential field is not allowed"):
        TaskEnvelopeV2.model_validate(payload)


def test_task_envelope_validates_trace_context_and_budget() -> None:
    payload = _task_payload()
    payload["traceparent"] = "not-a-traceparent"
    with pytest.raises(ValidationError):
        TaskEnvelopeV2.model_validate(payload)

    payload = _task_payload()
    payload["budget"] = {"max_execution_seconds": 0, "max_tool_calls": 100}
    with pytest.raises(ValidationError):
        TaskEnvelopeV2.model_validate(payload)


def test_result_requires_ordered_timestamps_and_keeps_legacy_evidence_separate() -> None:
    result = AgentResultV2.model_validate(_result_payload())
    assert result.status == TaskOutcome.SUCCEEDED

    payload = _result_payload()
    payload["completed_at"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValidationError, match="completed_at cannot be earlier"):
        AgentResultV2.model_validate(payload)

    payload = _result_payload()
    payload["legacy_evidence"] = {"authorization": "Bearer secret"}
    with pytest.raises(ValidationError, match="credential field is not allowed"):
        AgentResultV2.model_validate(payload)


def test_workflow_event_requires_versioned_identity_and_safe_payload() -> None:
    event = WorkflowEventV2.model_validate(_event_payload())
    assert event.event_type == "task.accepted"

    payload = _event_payload()
    payload["payload"] = {"credentials": {"password": "never-store"}}
    with pytest.raises(ValidationError, match="credential field is not allowed"):
        WorkflowEventV2.model_validate(payload)


def test_coding_task_adapter_round_trips_without_copying_arbitrary_metadata() -> None:
    task = CodingTask(
        objective="Implement a contract adapter",
        repository=RepositoryTarget(
            "StrategyG/RusaAi",
            "main",
            "feature/contract-adapter",
        ),
        acceptance_criteria=["Contract tests pass"],
        context="Preserve existing runtime behavior",
        metadata={"tenant_id": "tenant-a", "lease_token": "must-not-be-copied"},
    )
    envelope = coding_task_to_envelope(task)
    assert envelope.tenant_id == "tenant-a"
    assert envelope.input["repository"]["full_name"] == "StrategyG/RusaAi"
    assert "lease_token" not in envelope.model_dump(mode="json")

    restored = task_envelope_to_coding_task(envelope)
    assert restored.task_id == task.task_id
    assert restored.objective == task.objective
    assert restored.acceptance_criteria == task.acceptance_criteria
    assert restored.repository == task.repository
    assert restored.metadata == {"tenant_id": "tenant-a"}


def test_worker_result_adapter_preserves_legacy_data_without_claiming_qa_verdict() -> None:
    task = CodingTask(
        objective="Run tests",
        repository=RepositoryTarget("StrategyG/RusaAi"),
        acceptance_criteria=["Tests pass"],
    )
    envelope = coding_task_to_envelope(task)
    legacy = WorkerResult(
        role=WorkerRole.TESTER,
        summary="Pytest passed",
        success=True,
        artifacts=["reports/pytest.json"],
        evidence={"check:pytest": {"exit_code": 0}},
    )

    result = worker_result_to_agent_result(legacy, envelope)
    assert result.status == TaskOutcome.SUCCEEDED
    assert result.evidence_refs == []
    assert result.legacy_evidence == legacy.evidence

    restored = agent_result_to_worker_result(result, role=WorkerRole.TESTER)
    assert restored.success
    assert restored.artifacts == legacy.artifacts
    assert restored.evidence == legacy.evidence


def test_worker_result_with_blockers_maps_to_blocked() -> None:
    task = CodingTask(
        objective="Inspect code",
        repository=RepositoryTarget("StrategyG/RusaAi"),
        acceptance_criteria=["Inspection completes"],
    )
    envelope = coding_task_to_envelope(task)
    legacy = WorkerResult(
        role=WorkerRole.ANALYST,
        summary="Access unavailable",
        success=False,
        blockers=["Repository access denied"],
    )
    result = worker_result_to_agent_result(legacy, envelope)
    assert result.status == TaskOutcome.BLOCKED
    assert agent_result_to_worker_result(result, role=WorkerRole.ANALYST).blockers == [
        "Repository access denied"
    ]


def test_contract_json_schemas_match_models_and_validate_payloads() -> None:
    sample_payloads = {
        "agent-manifest.v1.schema.json": AgentManifestV1.model_validate(
            _manifest_payload()
        ).model_dump(mode="json"),
        "task-envelope.v2.schema.json": TaskEnvelopeV2.model_validate(
            _task_payload()
        ).model_dump(mode="json"),
        "agent-result.v2.schema.json": AgentResultV2.model_validate(
            _result_payload()
        ).model_dump(mode="json"),
        "workflow-event.v2.schema.json": WorkflowEventV2.model_validate(
            _event_payload()
        ).model_dump(mode="json"),
        "task-record.v2.schema.json": TaskRecordSnapshotV2(
            task=TaskEnvelopeV2.model_validate(_task_payload()),
            state=AICodeTaskStateV1.AWAITING_APPROVAL,
            updated_at=NOW,
            queue_status=AICodeQueueStatusV1.AWAITING_APPROVAL,
        ).model_dump(mode="json"),
    }
    schema_dir = Path("schemas/platform")

    for filename, (model, schema_id) in PLATFORM_SCHEMA_MODELS.items():
        expected = platform_contract_schema(model, schema_id)
        schema_path = schema_dir / filename
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert normalize_contract_schema(schema) == normalize_contract_schema(expected)

        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
        validator.validate(sample_payloads[filename])


def test_artifact_reference_rejects_malformed_sha256() -> None:
    with pytest.raises(ValidationError):
        ArtifactReference(
            uri="artifact://test/report.json",
            sha256="not-a-digest",
        )



def test_published_manifest_content_is_immutable_but_health_and_status_can_change() -> None:
    previous = AgentManifestV1.model_validate(_manifest_payload())
    candidate_payload = _manifest_payload()
    candidate_payload["status"] = "active"
    candidate_payload["health"] = {
        "status": "healthy",
        "checked_at": (NOW + timedelta(minutes=1)).isoformat(),
    }
    candidate_payload["updated_at"] = (NOW + timedelta(minutes=1)).isoformat()
    candidate = AgentManifestV1.model_validate(candidate_payload)
    validate_manifest_update(previous, candidate)

    changed = candidate.model_dump(mode="json")
    changed["tool_allowlist"] = ["delete_repository"]
    with pytest.raises(ValueError, match="published agent manifest content is immutable"):
        validate_manifest_update(candidate, AgentManifestV1.model_validate(changed))

    changed = candidate.model_dump(mode="json")
    changed["version"] = "2.0.0"
    with pytest.raises(ValueError, match="published agent version is immutable"):
        validate_manifest_update(candidate, AgentManifestV1.model_validate(changed))


def test_manifest_health_snapshot_has_bounded_status_and_timezone() -> None:
    payload = _manifest_payload()
    payload["health"] = {
        "status": AgentHealthStatus.DEGRADED.value,
        "checked_at": NOW.replace(tzinfo=None).isoformat(),
    }
    with pytest.raises(ValidationError, match="timestamp must include a timezone"):
        AgentManifestV1.model_validate(payload)


def test_task_envelope_preserves_approval_contract() -> None:
    envelope = TaskEnvelopeV2.model_validate(_task_payload())
    assert envelope.approval_required
    assert envelope.approval_ref == "approval://change-123"


def test_workflow_events_validate_known_payloads_and_ignore_unknown_optional_fields() -> None:
    event = WorkflowEventV2.model_validate(
        {
            "tenant_id": "tenant-a",
            "workflow_id": "workflow-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "attempt": 1,
            "correlation_id": "workflow-1",
            "event_type": "task.accepted",
            "producer_id": "aicode.worker.developer",
            "producer_version": "1.0.0",
            "occurred_at": NOW.isoformat(),
            "future_optional_field": "ignored by same-major consumer",
            "payload": {
                "worker_id": "worker-1",
                "lease_epoch": 1,
                "state_version": 2,
                "future_optional_field": {"new": "field"},
            },
        }
    )
    assert event.event_type == "task.accepted"

    invalid = {
        "tenant_id": "tenant-a",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "attempt": 1,
        "correlation_id": "workflow-1",
        "event_type": "task.accepted",
        "producer_id": "worker-1",
        "producer_version": "1.0.0",
        "occurred_at": NOW.isoformat(),
        "payload": {"worker_id": "worker-1", "lease_epoch": 0, "state_version": 1},
    }
    with pytest.raises(ValidationError):
        WorkflowEventV2.model_validate(invalid)


def test_validation_verdict_is_bound_to_exact_artifact_digest() -> None:
    digest = "sha256:" + "b" * 64
    evidence = EvidenceReference(
        uri="artifact://runs/run-1/test-report.json",
        sha256="sha256:" + "a" * 64,
        kind="test_report",
        producer_id="aicode.worker.tester",
        subject_digest=digest,
    )
    event = WorkflowEventV2.model_validate(
        {
            "tenant_id": "tenant-a",
            "workflow_id": "workflow-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "attempt": 1,
            "correlation_id": "workflow-1",
            "event_type": "validation.verdict",
            "producer_id": "qa-agent",
            "producer_version": "1.0.0",
            "occurred_at": NOW.isoformat(),
            "payload": {
                "verdict": "passed",
                "subject_digest": digest,
                "producer_id": "qa-agent",
                "test_profile": "release-gate-v1",
                "evidence_refs": [evidence.model_dump(mode="json")],
                "evaluated_at": NOW.isoformat(),
                "summary": "Exact artifact passed the release gate",
            },
        }
    )
    assert event.event_type == "validation.verdict"

    invalid_payload = {
        "verdict": "passed",
        "subject_digest": digest,
        "producer_id": "qa-agent",
        "test_profile": "release-gate-v1",
        "evidence_refs": [
            {
                "uri": "artifact://runs/run-1/test-report.json",
                "sha256": "sha256:" + "a" * 64,
                "kind": "test_report",
                "producer_id": "aicode.worker.tester",
                "subject_digest": "sha256:" + "c" * 64,
            }
        ],
        "evaluated_at": NOW.isoformat(),
        "summary": "Mismatched evidence must fail",
    }
    with pytest.raises(ValidationError, match="validation evidence subject digest"):
        WorkflowEventV2.model_validate(
            {
                "tenant_id": "tenant-a",
                "workflow_id": "workflow-1",
                "run_id": "run-1",
                "correlation_id": "workflow-1",
                "event_type": "validation.verdict",
                "producer_id": "qa-agent",
                "producer_version": "1.0.0",
                "occurred_at": NOW.isoformat(),
                "payload": invalid_payload,
            }
        )


def test_aicode_task_record_round_trip_preserves_state_results_approval_and_tenant() -> None:
    task = CodingTask(
        objective="Prepare a controlled repository change",
        repository=RepositoryTarget(
            "StrategyG/RusaAi",
            "main",
            "feature/contract-adapter",
        ),
        acceptance_criteria=["All contract tests pass"],
        context="Preserve current worker and queue semantics",
        metadata={
            "tenant_id": "tenant-a",
            "approval_required": True,
            "approval_ref": "approval://release/123",
            "policy_id": "release-policy",
            "policy_version": "3",
            "unapproved_extra": "must not cross the adapter",
        },
    )
    record = TaskRecord(
        task=task,
        state=TaskState.AWAITING_APPROVAL,
        results=[
            WorkerResult(
                role=WorkerRole.TESTER,
                summary="Tests passed",
                success=True,
                artifacts=["reports/test.json"],
                evidence={"check:pytest": {"exit_code": 0}},
            ),
            WorkerResult(
                role=WorkerRole.SECURITY,
                summary="Security checks passed",
                success=True,
                artifacts=["reports/security.json"],
                evidence={"check:bandit": {"exit_code": 0}},
            ),
        ],
        errors=["Awaiting release approval"],
        updated_at=(NOW + timedelta(minutes=2)).isoformat(),
    )

    snapshot = task_record_to_snapshot(
        record,
        queue_status=queue_status_from_legacy("awaiting_approval"),
    )
    assert snapshot.state == AICodeTaskStateV1.AWAITING_APPROVAL
    assert snapshot.task.approval_required
    assert snapshot.task.approval_ref == "approval://release/123"
    assert "unapproved_extra" not in snapshot.task.model_dump(mode="json")

    restored = snapshot_to_task_record(snapshot)
    assert restored.task.task_id == record.task.task_id
    assert restored.task.repository == record.task.repository
    assert restored.task.metadata == {
        "tenant_id": "tenant-a",
        "approval_required": True,
        "approval_ref": "approval://release/123",
        "policy_id": "release-policy",
        "policy_version": "3",
    }
    assert restored.state == record.state
    assert restored.errors == record.errors
    assert [item.role for item in restored.results] == [WorkerRole.TESTER, WorkerRole.SECURITY]
    assert [item.evidence for item in restored.results] == [
        item.evidence for item in record.results
    ]
    assert snapshot.queue_status == AICodeQueueStatusV1.AWAITING_APPROVAL


def test_all_six_worker_roles_and_queue_status_values_remain_compatible() -> None:
    for role in WorkerRole:
        task = CodingTask(
            objective=f"Check {role.value} compatibility",
            repository=RepositoryTarget("StrategyG/RusaAi"),
            acceptance_criteria=["Role identity preserved"],
        )
        envelope = coding_task_to_envelope(task)
        legacy = WorkerResult(
            role=role,
            summary=f"{role.value} completed",
            success=True,
            evidence={f"legacy:{role.value}": {"exit_code": 0}},
        )
        adapted = worker_result_to_agent_result(legacy, envelope)
        assert adapted.agent_id == f"aicode.worker.{role.value}"
        restored = agent_result_to_worker_result(adapted, role=role)
        assert restored.role == role
        assert restored.evidence == legacy.evidence

    statuses = ("queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled")
    assert [queue_status_to_legacy(queue_status_from_legacy(item)) for item in statuses] == list(statuses)



def test_approval_decision_and_policy_decision_are_typed_auditable_events() -> None:
    base = {
        "tenant_id": "tenant-a",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "attempt": 1,
        "correlation_id": "workflow-1",
        "producer_id": "policy-engine",
        "producer_version": "1.0.0",
        "occurred_at": NOW.isoformat(),
    }
    approval = WorkflowEventV2.model_validate(
        {
            **base,
            "event_type": "approval.decided",
            "payload": {
                "approval_id": "approval-123",
                "decision": "approved",
                "decided_by": "release-manager",
                "decided_at": NOW.isoformat(),
                "reason": "Evidence reviewed",
            },
        }
    )
    policy = WorkflowEventV2.model_validate(
        {
            **base,
            "event_type": "policy.decision",
            "payload": {
                "decision_id": "decision-123",
                "policy_id": "release-policy",
                "policy_version": "3",
                "decision": "require_approval",
                "action": "release.promote",
                "resource_ref": "artifact://release/candidate",
                "evaluated_at": NOW.isoformat(),
            },
        }
    )
    assert approval.event_type == "approval.decided"
    assert policy.event_type == "policy.decision"
