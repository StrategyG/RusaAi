from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from rusa_agents.devops.agent_registry import (
    AGENT_POLICIES,
    BUILD_AGENT,
    CODE_AGENT,
    PLAN_AGENT,
)
from rusa_agents.devops.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ContentAddressedArtifactStore,
)
from rusa_agents.devops.specialists import (
    CodeAgentExecutor,
    ExecutionLease,
    ExecutionLeaseError,
    PlanAgentExecutor,
    ScopedCodeWorkspaceGateway,
)
from rusa_agents.git_workspace import GitHubPublisher
from rusa_agents.tool_gateway import ToolDenied
from rusa_agents.contracts import WorkerRole


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _task(stage: str) -> dict:
    now = datetime.now(timezone.utc)
    agent_id = PLAN_AGENT if stage == "plan" else CODE_AGENT
    if stage == "plan":
        inputs = {"user_request": "Implement a safe example feature."}
        scope = ["plans/"]
    else:
        inputs = {
            "repository": "StrategyG/RusaAi",
            "base_branch": "main",
            "plan_ref": "artifact://sha256/" + "a" * 64,
            "acceptance_criteria": ["The example feature has a regression test."],
        }
        scope = ["src/", "tests/", "docs/"]
    return {
        "schema_version": 1,
        "task_id": f"task-{uuid4().hex[:12]}",
        "workflow_id": f"run-{uuid4().hex[:12]}",
        "tenant_id": "tenant-specialists",
        "agent_id": agent_id,
        "stage": stage,
        "objective": "Implement a bounded example feature.",
        "input": inputs,
        "constraints": {
            "allowed_tools": sorted(AGENT_POLICIES[agent_id].allowed_tools),
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": scope,
            "approval_required": False,
            "approval_scope": [],
            "budget": {
                "max_cost_usd": 2.0,
                "max_tool_calls": 20,
                "max_runtime_seconds": 300,
            },
        },
        "attempt": 1,
        "deadline_at": (now + timedelta(minutes=10)).isoformat(),
        "audit": {
            "created_at": now.isoformat(),
            "requested_by": "pytest",
            "policy_version": "policy-test-v1",
            "traceparent": TRACEPARENT,
        },
    }


def _lease(task: dict, *, principal: str | None = None, accepted: bool = True) -> ExecutionLease:
    return ExecutionLease(
        tenant_id=task["tenant_id"],
        run_id=task["workflow_id"],
        task_id=task["task_id"],
        attempt=task["attempt"],
        principal=principal or task["agent_id"],
        claim_id=f"claim-{uuid4().hex}",
        lease_epoch=1,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=2),
        accepted=accepted,
    )


def _valid_plan() -> dict:
    return {
        "title": "Example feature",
        "goal": "Deliver a bounded example feature.",
        "scope": {
            "in_scope": ["Implement the requested feature."],
            "out_of_scope": ["Production deployment."],
        },
        "requirements": [
            {"id": "REQ-1", "kind": "functional", "statement": "The feature works."}
        ],
        "tasks": [
            {
                "id": "TASK-1",
                "title": "Implement feature",
                "description": "Implement the source change and regression test.",
                "agent_id": CODE_AGENT,
                "depends_on": [],
            },
            {
                "id": "TASK-2",
                "title": "Build and test",
                "description": "Build and independently verify the exact source revision.",
                "agent_id": BUILD_AGENT,
                "depends_on": ["TASK-1"],
            },
        ],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "criterion": "The feature works as requested.",
                "verification": "Run the regression test against the built artifact.",
            }
        ],
        "risks": [
            {
                "id": "RISK-1",
                "severity": "low",
                "description": "The change may affect an existing API.",
                "mitigation": "Keep the API backward compatible and run regression tests.",
            }
        ],
        "approval_scopes": [],
    }


class FakeProvider:
    def __init__(self, payload: dict):
        self.payload = payload

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        return json.dumps(self.payload)


@pytest.mark.asyncio
async def test_plan_agent_creates_five_immutable_documents_and_evidence(tmp_path):
    task = _task("plan")
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    executor = PlanAgentExecutor(
        provider=FakeProvider(_valid_plan()),
        artifact_store=store,
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=PLAN_AGENT,
        lease=_lease(task),
    )

    assert outcome.result["status"] == "passed"
    assert len(outcome.result["output"]["artifact_refs"]) == 5
    assert len(outcome.result["output"]["evidence_refs"]) == 1
    assert outcome.result["handoff"] == {
        "next_agent": CODE_AGENT,
        "event": "plan.completed",
    }
    assert b"# Example feature" in store.get(
        task["tenant_id"], outcome.result["output"]["artifact_refs"][0]
    )
    evidence = store.get_json(
        task["tenant_id"], outcome.result["output"]["evidence_refs"][0]
    )
    assert evidence["task_id"] == task["task_id"]
    assert len(evidence["artifact_sha256"]) == 5


@pytest.mark.asyncio
async def test_plan_agent_rejects_dependency_cycles_without_passing(tmp_path):
    plan = _valid_plan()
    plan["tasks"][0]["depends_on"] = ["TASK-2"]
    task = _task("plan")
    executor = PlanAgentExecutor(
        provider=FakeProvider(plan),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "objects"),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=PLAN_AGENT,
        lease=_lease(task),
    )

    assert outcome.result["status"] == "failed"
    assert outcome.failure_category == "PERMANENT"
    assert outcome.result["output"]["findings"][0]["code"] == "plan.output_invalid"


@pytest.mark.asyncio
async def test_plan_agent_requires_matching_accepted_claim(tmp_path):
    task = _task("plan")
    executor = PlanAgentExecutor(
        provider=FakeProvider(_valid_plan()),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "objects"),
    )

    with pytest.raises(ExecutionLeaseError, match="accepted durable claim"):
        await executor.execute(
            task,
            authenticated_principal=PLAN_AGENT,
            lease=_lease(task, accepted=False),
        )

    with pytest.raises(ExecutionLeaseError, match="identity"):
        await executor.execute(
            task,
            authenticated_principal=PLAN_AGENT,
            lease=_lease(task, principal=CODE_AGENT),
        )


def test_artifact_store_is_immutable_and_tenant_scoped(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    first = store.put("tenant-a", b"immutable plan")
    second = store.put("tenant-a", b"immutable plan")

    assert first == second
    assert store.get("tenant-a", first) == b"immutable plan"
    with pytest.raises(ArtifactNotFoundError):
        store.get("tenant-b", first)


def test_artifact_store_rejects_noncanonical_refs_and_detects_corruption(tmp_path):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    reference = store.put("tenant-a", b"evidence")
    with pytest.raises(ValueError, match="artifact reference"):
        store.get("tenant-a", "artifact://sha256/../secrets")

    digest = reference.rsplit("/", 1)[-1]
    object_path = store._object_path("tenant-a", "artifact", digest)
    object_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="digest"):
        store.get("tenant-a", reference)


def test_scoped_code_gateway_enforces_actual_write_paths_and_capabilities():
    task = _task("code")
    task["constraints"]["write_scope"] = ["src/"]

    class BaseGateway:
        def execute(self, action, role):
            return "executed"

        def git_diff(self):
            return "diff"

    gateway = ScopedCodeWorkspaceGateway(BaseGateway(), task)
    assert gateway.execute(
        {"name": "write_file", "arguments": {"path": "src/main.py", "content": "x"}},
        WorkerRole.DEVELOPER,
    ) == "executed"

    with pytest.raises(ToolDenied, match="write_scope"):
        gateway.execute(
            {"name": "write_file", "arguments": {"path": "tests/test_app.py", "content": "x"}},
            WorkerRole.DEVELOPER,
        )
    with pytest.raises(ToolDenied, match="not registered"):
        gateway.execute(
            {"name": "run_check", "arguments": {"profile": "pytest"}},
            WorkerRole.DEVELOPER,
        )


class FakeWorkspaceManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.cleaned = False
        self.created_task = None

    def create(self, task, tenant_id):
        self.created_task = task
        self.workspace.mkdir(parents=True, exist_ok=True)
        return self.workspace

    def cleanup(self, task, tenant_id):
        self.cleaned = True


class FakePublisher:
    def __init__(self, *, fail_pr: bool = False):
        self.fail_pr = fail_pr
        self.pushed = None

    def commit_and_push(self, workspace, repository, branch, message):
        self.pushed = (repository, branch, message)
        return "c" * 40

    async def create_or_get_draft_pr(self, repository, branch, base, title, body):
        if self.fail_pr:
            raise RuntimeError("simulated GitHub outage")
        return f"https://github.com/{repository}/pull/123"


class FakeCodeWorker:
    def __init__(self, workspace: Path, *, succeed: bool = True):
        self.workspace = workspace
        self.succeed = succeed

    async def run(self, task, prior_results):
        if self.succeed:
            target = self.workspace / "src" / "main.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def feature(): return True\n", encoding="utf-8")
            return SimpleNamespace(success=True, blockers=[])
        return SimpleNamespace(success=False, blockers=["model did not finish"])


@pytest.mark.asyncio
async def test_code_agent_publishes_only_task_feature_branch_and_draft_pr(tmp_path, monkeypatch):
    task = _task("code")
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    task["input"]["plan_ref"] = store.put(
        task["tenant_id"], b"# Approved plan\nImplement a small feature.", namespace="artifact"
    )
    workspace = tmp_path / "worktree"
    manager = FakeWorkspaceManager(workspace)
    publisher = FakePublisher()
    monkeypatch.setattr(
        GitHubPublisher,
        "_safe_changed_paths",
        staticmethod(lambda path: ["src/main.py"]),
    )
    executor = CodeAgentExecutor(
        provider=FakeProvider({}),
        artifact_store=store,
        workspace_manager=manager,
        publisher=publisher,
        gateway_factory=lambda path, assigned_task, checkpoint: SimpleNamespace(workspace=path),
        worker_factory=lambda provider, gateway: FakeCodeWorker(gateway.workspace),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=CODE_AGENT,
        lease=_lease(task),
    )

    assert outcome.result["status"] == "passed"
    assert outcome.result["audit"]["source_commit"] == "c" * 40
    assert outcome.result["handoff"]["next_agent"] == BUILD_AGENT
    assert publisher.pushed[1].startswith("feature/rusa-devops-")
    assert publisher.pushed[1] not in {"main", "master"}
    assert manager.cleaned is True
    assert any("pull/123" in ref for ref in outcome.result["output"]["artifact_refs"])


@pytest.mark.asyncio
async def test_code_agent_records_applied_side_effect_if_draft_pr_creation_fails(tmp_path, monkeypatch):
    task = _task("code")
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    task["input"]["plan_ref"] = store.put(
        task["tenant_id"], b"# Approved plan", namespace="artifact"
    )
    workspace = tmp_path / "worktree"
    manager = FakeWorkspaceManager(workspace)
    publisher = FakePublisher(fail_pr=True)
    monkeypatch.setattr(
        GitHubPublisher,
        "_safe_changed_paths",
        staticmethod(lambda path: ["src/main.py"]),
    )
    executor = CodeAgentExecutor(
        provider=FakeProvider({}),
        artifact_store=store,
        workspace_manager=manager,
        publisher=publisher,
        gateway_factory=lambda path, assigned_task, checkpoint: SimpleNamespace(workspace=path),
        worker_factory=lambda provider, gateway: FakeCodeWorker(gateway.workspace),
    )

    outcome = await executor.execute(
        task,
        authenticated_principal=CODE_AGENT,
        lease=_lease(task),
    )

    assert outcome.result["status"] == "failed"
    assert outcome.failure_category == "AMBIGUOUS"
    assert outcome.side_effect == "APPLIED"
    assert outcome.result["output"]["findings"][0]["code"] == "code.draft_pr_creation_failed"
    assert manager.cleaned is True
