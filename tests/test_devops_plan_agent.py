from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from rusa_agents.devops.artifacts import ArtifactIntegrityError, FilesystemArtifactStore
from rusa_agents.devops.contracts import validate_agent_result_for_task
from rusa_agents.devops.plan_agent import PlanAgent


def _task(**overrides):
    now = datetime.now(timezone.utc)
    task = {
        "schema_version": 1,
        "task_id": str(uuid4()),
        "workflow_id": str(uuid4()),
        "tenant_id": "tenant-plan-test",
        "agent_id": "rusa_plan_agent",
        "stage": "plan",
        "objective": "Prepare an implementable and verifiable software delivery plan.",
        "input": {
            "user_request": "Add a tenant-safe audit export endpoint with tests."
        },
        "constraints": {
            "allowed_tools": ["plan.write", "artifact.write"],
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": [],
            "approval_required": False,
            "approval_scope": [],
            "budget": {
                "max_cost_usd": 1.0,
                "max_tool_calls": 0,
                "max_runtime_seconds": 300,
            },
        },
        "attempt": 1,
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
        "audit": {
            "created_at": now.isoformat(),
            "requested_by": "test-owner",
            "policy_version": "plan-policy-v1",
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        },
    }
    for key, value in overrides.items():
        task[key] = value
    return task


def _plan(**overrides):
    plan = {
        "schema_version": 1,
        "summary": "Deliver a tenant-safe audit export endpoint with independently verified evidence.",
        "goal": "Allow authorized tenant administrators to export audit records safely.",
        "scope": {
            "in_scope": ["Add a tenant-scoped export endpoint.", "Add authorization and audit tests."],
            "out_of_scope": ["Change global retention policy."],
        },
        "requirements": {
            "functional": [
                {
                    "id": "REQ-FUNC-1",
                    "description": "An authorized tenant administrator can export only its own records.",
                    "priority": "P1",
                }
            ],
            "nonfunctional": [
                {
                    "id": "REQ-NFR-1",
                    "description": "The export is bounded by a configured maximum size.",
                    "priority": "P1",
                }
            ],
        },
        "tasks": [
            {
                "id": "TASK-CODE-1",
                "title": "Implement the export endpoint",
                "description": "Implement a tenant-scoped endpoint and bounded serialization.",
                "stage": "code",
                "agent_id": "rusa_code_agent",
                "depends_on": [],
                "acceptance_criteria": ["The endpoint rejects cross-tenant access."],
            },
            {
                "id": "TASK-TEST-1",
                "title": "Verify export authorization",
                "description": "Add unit and integration coverage for tenant isolation.",
                "stage": "test",
                "agent_id": "rusa_test_agent",
                "depends_on": ["TASK-CODE-1"],
                "acceptance_criteria": ["A cross-tenant request is denied."],
            },
        ],
        "acceptance_criteria": [
            {
                "id": "AC-AUTH-1",
                "criterion": "An authenticated tenant cannot export another tenant's records.",
                "verification": "Integration test with two tenant identities.",
            }
        ],
        "dependencies": [],
        "risks": [],
        "approval_recommended": False,
        "approval_reasons": [],
    }
    plan.update(overrides)
    return plan


class FakeProvider:
    model = "test/plan-model"

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def complete(self, *, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return self.response


class FakeContextProvider:
    async def load(self, task):
        return {
            "repository": task["input"]["repository"],
            "commit_sha": task["input"]["commit_sha"],
            "sources": [
                {
                    "ref": "repo://README.md",
                    "content": "Existing service uses tenant-scoped authorization.",
                    "trust": "untrusted_repository_content",
                }
            ],
        }


@pytest.mark.asyncio
async def test_plan_agent_persists_five_tenant_private_artifacts_and_validates_result(tmp_path):
    task = _task()
    provider = FakeProvider(json.dumps(_plan()))
    store = FilesystemArtifactStore(tmp_path)
    agent = PlanAgent(provider=provider, artifact_store=store)

    result = await agent.run(task)

    validate_agent_result_for_task(task, result)
    assert result["status"] == "passed"
    assert result["handoff"] == {
        "next_agent": "rusa_code_agent",
        "event": "plan.completed",
    }
    assert len(result["output"]["artifact_refs"]) == 5
    assert len(result["output"]["evidence_refs"]) == 3

    contents = {}
    for ref in result["output"]["artifact_refs"]:
        content, manifest = store.read(tenant_id=task["tenant_id"], uri=ref)
        contents[manifest["name"]] = content.decode("utf-8")
        assert manifest["workflow_id"] == task["workflow_id"]
        assert manifest["task_id"] == task["task_id"]
        assert manifest["access_policy"] == "tenant-private"
        assert manifest["lineage"]["source_commit"] is None

    assert "# Delivery plan" in contents["plan.md"]
    assert "REQ-FUNC-1" in contents["requirements.md"]
    assert "TASK-TEST-1" in contents["backlog.yaml"]
    assert "AC-AUTH-1" in contents["acceptance-criteria.md"]
    assert "Risk assessment" in contents["risk-assessment.md"]


@pytest.mark.asyncio
async def test_mandatory_policy_approval_creates_pending_artifact_and_stops_handoff(tmp_path):
    task = _task()
    task["constraints"]["approval_required"] = True
    provider = FakeProvider(json.dumps(_plan()))
    store = FilesystemArtifactStore(tmp_path)
    result = await PlanAgent(provider=provider, artifact_store=store).run(task)

    assert result["status"] == "needs_approval"
    assert result["output"]["approval_ref"] == result["output"]["artifact_refs"][-1]
    assert "handoff" not in result
    content, manifest = store.read(
        tenant_id=task["tenant_id"],
        uri=result["output"]["approval_ref"],
    )
    assert manifest["name"] == "approval-request.md"
    assert b"PENDING" in content
    assert b"no approval is implied" in content


@pytest.mark.asyncio
async def test_model_can_only_add_a_conservative_approval_hold(tmp_path):
    task = _task()
    plan = _plan(
        approval_recommended=True,
        approval_reasons=["The requested change may alter protected audit retention."],
    )
    result = await PlanAgent(
        provider=FakeProvider(json.dumps(plan)),
        artifact_store=FilesystemArtifactStore(tmp_path),
    ).run(task)

    assert result["status"] == "needs_approval"
    assert "handoff" not in result
    assert result["output"]["approval_ref"]


@pytest.mark.asyncio
async def test_risk_level_approval_flag_cannot_be_downgraded_by_model(tmp_path):
    task = _task()
    plan = _plan(
        risks=[{
            "id": "RISK-PROD-1",
            "description": "The change may affect production data.",
            "likelihood": "medium",
            "impact": "critical",
            "mitigation": "Require an approved production change window.",
            "approval_required": True,
        }]
    )
    result = await PlanAgent(
        provider=FakeProvider(json.dumps(plan)),
        artifact_store=FilesystemArtifactStore(tmp_path),
    ).run(task)

    assert result["status"] == "needs_approval"
    assert "handoff" not in result


@pytest.mark.asyncio
async def test_repository_planning_requires_pinned_revision_and_authorized_context(tmp_path):
    task = _task()
    task["input"].update(
        repository="StrategyG/RusaAi",
        commit_sha="a" * 40,
    )
    task["constraints"]["allowed_tools"].append("repository.read")
    provider = FakeProvider(json.dumps(_plan()))
    result = await PlanAgent(
        provider=provider,
        artifact_store=FilesystemArtifactStore(tmp_path),
    ).run(task)

    assert result["status"] == "blocked"
    assert result["output"]["findings"][0]["code"] == "plan.context_unavailable"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_repository_context_is_pinned_and_delimited_as_untrusted_data(tmp_path):
    task = _task()
    task["input"].update(
        repository="StrategyG/RusaAi",
        commit_sha="a" * 40,
    )
    task["constraints"]["allowed_tools"].append("repository.read")
    provider = FakeProvider(json.dumps(_plan()))
    result = await PlanAgent(
        provider=provider,
        artifact_store=FilesystemArtifactStore(tmp_path),
        context_provider=FakeContextProvider(),
    ).run(task)

    assert result["status"] == "passed"
    system_prompt, user_prompt = provider.calls[0]
    assert "untrusted data" in system_prompt
    assert "untrusted_repository_content" in user_prompt
    assert result["audit"]["source_commit"] == "a" * 40
    _, manifest = FilesystemArtifactStore(tmp_path).read(
        tenant_id=task["tenant_id"],
        uri=result["output"]["artifact_refs"][0],
    )
    assert manifest["lineage"]["source_commit"] == "a" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,expected_code",
    [
        (
            lambda task: task["constraints"]["allowed_tools"].append("deployment.execute"),
            "plan.capability_exceeds_allowlist",
        ),
        (
            lambda task: task["constraints"].update(network_access=True),
            "plan.network_access_denied",
        ),
        (
            lambda task: task["constraints"].update(write_scope=["src/"]),
            "plan.repository_write_denied",
        ),
    ],
)
async def test_plan_agent_fails_closed_on_capability_network_or_write_scope(
    tmp_path, mutation, expected_code
):
    task = _task()
    mutation(task)
    provider = FakeProvider(json.dumps(_plan()))
    result = await PlanAgent(
        provider=provider,
        artifact_store=FilesystemArtifactStore(tmp_path),
    ).run(task)

    assert result["status"] == "blocked"
    assert result["output"]["findings"][0]["code"] == expected_code
    assert provider.calls == []


@pytest.mark.asyncio
async def test_plan_agent_rejects_dependency_cycles_before_writing_artifacts(tmp_path):
    task = _task()
    plan = _plan()
    plan["tasks"][0]["depends_on"] = ["TASK-TEST-1"]
    provider = FakeProvider(json.dumps(plan))
    store = FilesystemArtifactStore(tmp_path)
    result = await PlanAgent(provider=provider, artifact_store=store).run(task)

    assert result["status"] == "failed"
    assert result["output"]["findings"][0]["code"] == "plan.invalid_or_unavailable_model_output"
    assert list(Path(tmp_path).rglob("*.json")) == []


@pytest.mark.asyncio
async def test_plan_agent_rejects_invalid_model_output_without_claiming_pass(tmp_path):
    task = _task()
    result = await PlanAgent(
        provider=FakeProvider('{"schema_version":1,"summary":"incomplete"}'),
        artifact_store=FilesystemArtifactStore(tmp_path),
    ).run(task)

    assert result["status"] == "failed"
    assert result["output"]["evidence_refs"] == []
    assert result["output"]["findings"]


def test_artifact_store_is_tenant_scoped_and_detects_tampering(tmp_path):
    store = FilesystemArtifactStore(tmp_path)
    record = store.put(
        tenant_id="tenant-one",
        workflow_id="run-one",
        task_id="task-one",
        name="plan.md",
        content="# Tenant one plan\n",
        content_type="text/markdown",
    )

    with pytest.raises(ArtifactIntegrityError, match="authenticated tenant"):
        store.read(tenant_id="tenant-two", uri=record.uri)

    tenant_key = record.uri.split("://", 1)[1].split("/", 1)[0]
    artifact_id = record.uri.split("/", 3)[-1].split("?", 1)[0]
    blob = Path(tmp_path) / tenant_key / "blobs" / record.sha256
    blob.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="integrity check"):
        store.read(tenant_id="tenant-one", uri=record.uri)
