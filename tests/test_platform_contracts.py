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
    task_envelope_to_coding_task,
    worker_result_to_agent_result,
)
from rusa_agents.contracts import CodingTask, RepositoryTarget, WorkerResult, WorkerRole
from rusa_agents.platform_contracts import (
    PLATFORM_SCHEMA_MODELS,
    AgentDomain,
    AgentManifestV1,
    AgentResultV2,
    AgentStatus,
    ArtifactReference,
    ExecutionBudget,
    RuntimeBinding,
    RuntimeKind,
    TaskEnvelopeV2,
    TaskOutcome,
    WorkflowEventV2,
    platform_contract_schema,
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
        "event_type": "task.succeeded",
        "producer_id": "aicode.worker.tester",
        "producer_version": "1.0.0",
        "occurred_at": NOW.isoformat(),
        "payload": {"status": "succeeded"},
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
    assert event.event_type == "task.succeeded"

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
    }
    schema_dir = Path("schemas/platform")

    for filename, (model, schema_id) in PLATFORM_SCHEMA_MODELS.items():
        expected = json.dumps(
            platform_contract_schema(model, schema_id),
            indent=2,
            sort_keys=True,
        ) + "\n"
        schema_path = schema_dir / filename
        assert schema_path.read_text(encoding="utf-8") == expected

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
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
