"""Tests for the staged Agent Runtime v1.0 registry, router, DAG and executor."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from rusa_agents.audit import AuditLog
from rusa_agents.capability_registry import (
    AgentWorkerRegistry,
    RegistryNotActivatedError,
    RegistryValidationError,
)
from rusa_agents.capability_router import CapabilityRouter, RouteRequest
from rusa_agents.contracts import CodingTask, RepositoryTarget, WorkerRole
from rusa_agents.memory import SQLiteMemory
from rusa_agents.queue import Job
from rusa_agents.runtime import AICodeRuntime
from rusa_agents.runtime_v1 import AgentRuntimeV1
from rusa_agents.task_decomposer import TaskDecomposer, TaskNode, TaskPlan
from rusa_agents.worker_execution import (
    CheckpointConflictError,
    InMemoryCheckpointStore,
    LeaseBoundCheckpointStore,
    NodeStatus,
    PlanStatus,
    PolicyDecision,
    TaskMemoryCheckpointStore,
    VerificationDecision,
    WorkerExecutionEngine,
    WorkerOutput,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "rusa_agents" / "data"
REGISTRY_PATH = DATA_DIR / "agent_worker_registry.v1.json"
SCHEMA_PATH = DATA_DIR / "agent_worker_registry.v1.schema.json"


def _registry(active: bool = False) -> AgentWorkerRegistry:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if active:
        data["status"] = "active"
        data["source_chunk_map_status"] = "approved_for_routing"
        for index, role in enumerate(data["roles"], start=900):
            role["registration_status"] = "registered"
            role["runtime_status"] = "verified"
            role["activation_state"] = "active"
            # Test-only source IDs exercise the active-registry validation gate.
            role["source_chunk_ids"] = [f"RUSA-KB-{index:03d}"]
        data["counts"]["source_chunk_refs_total"] = len(data["roles"])
    return AgentWorkerRegistry.from_mapping(data, schema)


def _node(
    task_id: str,
    role_id: str,
    dependencies: tuple[str, ...] = (),
    *,
    risk_class: str = "low",
    parent_task_id: str | None = None,
    deadline_at: str | None = None,
    delegated_by_role_id: str = "supreme_orchestrator",
) -> TaskNode:
    return TaskNode(
        task_id=task_id,
        selected_role_id=role_id,
        scope="worker",
        objective=f"Execute {role_id}",
        delegated_by_role_id=delegated_by_role_id,
        acceptance_criteria=("Return verified evidence",),
        dependencies=dependencies,
        parent_task_id=parent_task_id,
        deadline_at=deadline_at,
        risk_class=risk_class,
    )


def _plan(*nodes: TaskNode) -> TaskPlan:
    return TaskPlan(
        root_task_id="root-task-001",
        tenant_id="tenant-a",
        objective="Validate the Agent Runtime DAG",
        nodes=tuple(nodes),
    )


def test_default_registry_is_structurally_valid_but_not_active() -> None:
    registry = AgentWorkerRegistry.load()
    summary = registry.summary()
    assert summary["role_count"] == 24
    assert summary["agents"] == 10
    assert summary["aicode_workers"] == 6
    assert summary["devops_lifecycle_profiles"] == 8
    assert summary["dispatchable_roles"] == 0
    assert summary["execution_enabled"] is False
    assert summary["provisional_capability_id_count"] == 34
    assert summary["source_chunk_map_status"] == "candidate_crosswalk_pending_review"
    assert summary["source_chunk_reference_count"] == 24


def test_runtime_validates_existing_aicode_worker_bindings_without_activation() -> None:
    runtime = AgentRuntimeV1()
    result = runtime.validate_aicode_worker_bindings(
        ("analyst", "architect", "developer", "debugger", "tester", "security")
    )
    assert result["compatible"] is True
    assert result["dispatchable"] is False
    assert result["worker_role_ids"] == [
        "analyst", "architect", "developer", "debugger", "tester", "security"
    ]
    with pytest.raises(RegistryValidationError, match="not registered as an AICode worker"):
        runtime.validate_aicode_worker_bindings(("supreme_orchestrator",))


def test_task_memory_checkpoint_store_is_tenant_scoped_and_durable(tmp_path) -> None:
    memory = SQLiteMemory(str(tmp_path / "agent-runtime-memory.sqlite3"))
    store = TaskMemoryCheckpointStore(memory)
    payload = {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "plan_fingerprint": "fingerprint-a",
        "node_results": [],
    }
    store.save("root-task-001", "tenant-a", payload)
    restored = store.load("root-task-001", "tenant-a")
    assert restored["schema_version"] == payload["schema_version"]
    assert restored["checkpoint_version"] == 1
    assert store.load("root-task-001", "tenant-b") is None
    reopened = TaskMemoryCheckpointStore(
        SQLiteMemory(str(tmp_path / "agent-runtime-memory.sqlite3"))
    )
    reopened_payload = reopened.load("root-task-001", "tenant-a")
    assert reopened_payload["plan_fingerprint"] == payload["plan_fingerprint"]
    assert reopened_payload["checkpoint_version"] == 1


def test_task_memory_checkpoint_compare_and_set_rejects_stale_writers(tmp_path) -> None:
    database = str(tmp_path / "checkpoint-cas.sqlite3")
    first_writer = TaskMemoryCheckpointStore(SQLiteMemory(database))
    stale_writer = TaskMemoryCheckpointStore(SQLiteMemory(database))
    payload = {
        "schema_version": "1.0",
        "tenant_id": "tenant-a",
        "plan_fingerprint": "fingerprint-first",
        "node_results": [],
    }

    assert first_writer.compare_and_set(
        "root-task-cas", "tenant-a", payload, expected_version=0
    ) == 1
    with pytest.raises(CheckpointConflictError, match="expected version 0"):
        stale_writer.compare_and_set(
            "root-task-cas",
            "tenant-a",
            {**payload, "plan_fingerprint": "stale-overwrite"},
            expected_version=0,
        )

    current = stale_writer.load("root-task-cas", "tenant-a")
    assert current["plan_fingerprint"] == "fingerprint-first"
    assert current["checkpoint_version"] == 1
    assert stale_writer.compare_and_set(
        "root-task-cas", "tenant-a", payload, expected_version=1
    ) == 2
    assert stale_writer.load("root-task-cas", "tenant-b") is None


def test_task_memory_checkpoint_cas_cannot_overwrite_legacy_task_state(tmp_path) -> None:
    memory = SQLiteMemory(str(tmp_path / "checkpoint-legacy.sqlite3"))
    memory.save(
        "root-task-legacy",
        "StrategyG/RusaAi",
        "aicode_task_state",
        {"legacy": True},
        tenant_id="tenant-a",
    )
    store = TaskMemoryCheckpointStore(memory)
    with pytest.raises(CheckpointConflictError, match="expected version 0"):
        store.compare_and_set(
            "root-task-legacy",
            "tenant-a",
            {"schema_version": "1.0", "tenant_id": "tenant-a"},
            expected_version=0,
        )
    assert memory.get("root-task-legacy", "tenant-a")["state"] == "aicode_task_state"


def test_lease_bound_checkpoint_store_checks_tenant_and_lease() -> None:
    class StubLeaseQueue:
        def __init__(self):
            self.valid = True
            self.calls = 0

        def with_lease_owned(
            self, job_id, tenant_id, worker_id, lease_token, operation, *, lease_seconds
        ):
            self.calls += 1
            if not self.valid:
                raise RuntimeError("lease lost")
            return operation()

    delegate = InMemoryCheckpointStore()
    queue = StubLeaseQueue()
    store = LeaseBoundCheckpointStore(
        delegate,
        queue,
        job_id=7,
        task_id="root-task-001",
        tenant_id="tenant-a",
        worker_id="worker-a",
        lease_token="lease-token",
    )
    payload = {"tenant_id": "tenant-a", "plan_fingerprint": "fingerprint"}
    store.save("root-task-001", "tenant-a", payload)
    restored = store.load("root-task-001", "tenant-a")
    assert restored["plan_fingerprint"] == payload["plan_fingerprint"]
    assert restored["checkpoint_version"] == 1
    assert queue.calls == 2
    with pytest.raises(PermissionError, match="Checkpoint tenant"):
        store.load("root-task-001", "tenant-b")
    with pytest.raises(PermissionError, match="Checkpoint task"):
        store.load("other-task", "tenant-a")
    queue.valid = False
    with pytest.raises(RuntimeError, match="lease lost"):
        store.save("root-task-001", "tenant-a", {"stale": True})
    persisted = delegate.load("root-task-001", "tenant-a")
    assert persisted["plan_fingerprint"] == payload["plan_fingerprint"]
    assert persisted["checkpoint_version"] == 1



def test_registry_rejects_activation_without_canonical_source_chunks() -> None:
    registry = _registry()
    data = registry.raw_data
    data["status"] = "active"
    data["source_chunk_map_status"] = "approved_for_routing"
    data["counts"]["source_chunk_refs_total"] = 0
    for role in data["roles"]:
        role["registration_status"] = "registered"
        role["runtime_status"] = "verified"
        role["activation_state"] = "active"
        role["source_chunk_ids"] = []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(RegistryValidationError, match="canonical source_chunk_ids"):
        AgentWorkerRegistry.from_mapping(data, schema)



def test_registry_rejects_activation_before_source_crosswalk_approval() -> None:
    registry = _registry()
    data = registry.raw_data
    data["status"] = "active"
    for role in data["roles"]:
        role["registration_status"] = "registered"
        role["runtime_status"] = "verified"
        role["activation_state"] = "active"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(RegistryValidationError, match="source_chunk_map_status"):
        AgentWorkerRegistry.from_mapping(data, schema)


def test_registry_rejects_duplicate_role_ids() -> None:
    registry = _registry()
    data = registry.raw_data
    data["roles"][1]["role_id"] = data["roles"][0]["role_id"]
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(RegistryValidationError, match="unique"):
        AgentWorkerRegistry.from_mapping(data, schema)


def test_draft_router_returns_preview_without_tool_grants() -> None:
    router = CapabilityRouter(_registry())
    decision = router.route(
        RouteRequest(
            scope="worker",
            required_capability_ids=("RUSA-AW-WORKER-001",),
            identity_type="aicode_worker",
        )
    )
    assert decision.status == "PREVIEW_ONLY"
    assert decision.selected_role_id is None
    assert decision.suggested_role_id == "analyst"
    assert decision.tool_grants == ()


def test_active_router_dispatches_only_verified_role() -> None:
    router = CapabilityRouter(_registry(active=True))
    decision = router.route(
        RouteRequest(
            scope="worker",
            required_capability_ids=("RUSA-AW-WORKER-001",),
            identity_type="aicode_worker",
        )
    )
    assert decision.status == "ROUTED"
    assert decision.selected_role_id == "analyst"


def test_devops_profile_must_be_routed_under_devops_parent() -> None:
    router = CapabilityRouter(_registry(active=True))
    correct = router.route(
        RouteRequest(
            scope="worker",
            explicit_role_id="rusa_plan_agent",
            parent_role_id="devops",
        )
    )
    incorrect = router.route(
        RouteRequest(
            scope="worker",
            explicit_role_id="rusa_plan_agent",
            parent_role_id="supreme_orchestrator",
        )
    )
    assert correct.selected_role_id == "rusa_plan_agent"
    assert incorrect.status == "NO_MATCH"


def test_router_rejects_unknown_capability_ids() -> None:
    router = CapabilityRouter(_registry())
    with pytest.raises(ValueError, match="Unknown capability"):
        router.route(RouteRequest(scope="worker", required_capability_ids=("UNKNOWN-1",)))


def test_task_decomposer_returns_stable_topological_order() -> None:
    registry = _registry(active=True)
    plan = _plan(
        _node("task-analyst", "analyst"),
        _node("task-developer", "developer", ("task-analyst",)),
        _node("task-tester", "tester", ("task-developer",)),
    )
    validation = TaskDecomposer(registry).validate(plan)
    assert validation.topological_order == (
        "task-analyst", "task-developer", "task-tester"
    )


def test_task_decomposer_rejects_cycles() -> None:
    decomposer = TaskDecomposer(_registry(active=True))
    plan = _plan(
        _node("task-a", "analyst", ("task-b",)),
        _node("task-b", "developer", ("task-a",)),
    )
    with pytest.raises(ValueError, match="cycle"):
        decomposer.validate(plan)


def test_task_decomposer_rejects_unknown_role_and_scope_mismatch() -> None:
    decomposer = TaskDecomposer(_registry(active=True))
    with pytest.raises(KeyError, match="Unknown"):
        decomposer.validate(_plan(_node("task-x", "missing_role")))
    bad_scope = TaskNode(
        task_id="task-y",
        selected_role_id="supreme_orchestrator",
        scope="worker",
        objective="Wrong scope",
        delegated_by_role_id="supreme_orchestrator",
        acceptance_criteria=("Verify",),
    )
    with pytest.raises(ValueError, match="scope does not match"):
        decomposer.validate(_plan(bad_scope))


def test_devops_profile_requires_change_and_rollback_contract() -> None:
    node = _node(
        "task-plan",
        "rusa_plan_agent",
        delegated_by_role_id="devops",
    )
    with pytest.raises(ValueError, match="DevOps profile requires"):
        TaskDecomposer(_registry(active=True)).validate(_plan(node))


class _Handler:
    def __init__(self, output: WorkerOutput | None = None, fail: bool = False):
        self.output = output or WorkerOutput(
            summary="completed",
            evidence_refs=("evidence://test",),
            tests_or_checks={"check": {"exit_code": 0}},
        )
        self.fail = fail
        self.calls = 0

    async def execute(self, node, dependency_results):
        self.calls += 1
        if self.fail:
            raise RuntimeError("temporary failure")
        return self.output


def _authorizer(node, tenant_id):
    return PolicyDecision(allowed=True)


def _verifier(node, output):
    return VerificationDecision(accepted=bool(output.evidence_refs), reason="evidence required")


def _agent_runtime_queue_job(payload: dict) -> Job:
    return Job(
        id=901,
        task_id="root-task-queue",
        tenant_id="tenant-a",
        payload=payload,
        attempts=1,
        max_attempts=3,
        lease_owner="worker-queue",
        lease_token="00000000-0000-4000-8000-000000000901",
    )


def test_agent_runtime_coding_task_enforces_tenant_repository_scope() -> None:
    job = _agent_runtime_queue_job({
        "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
        "context": "Reviewed project context",
    })
    plan = TaskPlan(
        root_task_id=job.task_id,
        tenant_id=job.tenant_id,
        objective="Implement a tenant-scoped change",
        nodes=(_node("node_repo_scope", "analyst"),),
    )
    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.config = SimpleNamespace(
        environment="production",
        tenant_repositories={"tenant-a": ["StrategyG/RusaAi"]},
    )

    task = runtime._agent_runtime_coding_task(job, plan)

    assert task.repository.full_name == "StrategyG/RusaAi"
    assert task.repository.base_branch == "main"
    assert task.repository.work_branch.startswith("feature/aicode/root-task-q")
    assert task.context == "Reviewed project context"
    assert task.metadata["tenant_id"] == "tenant-a"

    runtime.config = SimpleNamespace(
        environment="production",
        tenant_repositories={"tenant-a": ["Other/Repo"]},
    )
    with pytest.raises(PermissionError, match="not authorized"):
        runtime._agent_runtime_coding_task(job, plan)


def test_agent_runtime_policy_and_result_verification_fail_closed() -> None:
    normal = AICodeRuntime._agent_runtime_policy_authorizer(
        _node("node-policy", "developer"), "tenant-a"
    )
    high_risk = AICodeRuntime._agent_runtime_policy_authorizer(
        _node("node-risk", "developer", risk_class="high"), "tenant-a"
    )
    unauthorized = TaskNode(
        task_id="node-permission",
        selected_role_id="analyst",
        scope="worker",
        objective="Attempt a protected action",
        delegated_by_role_id="supreme_orchestrator",
        acceptance_criteria=("Do not deploy",),
        requested_permissions=("deploy",),
    )
    denied = AICodeRuntime._agent_runtime_policy_authorizer(unauthorized, "tenant-a")

    assert normal.allowed is True
    assert high_risk.allowed is False and high_risk.requires_approval is True
    assert denied.allowed is False and "exceed" in denied.reason

    security_node = _node("node-security", "security")
    incomplete = AICodeRuntime._agent_runtime_result_verifier(
        security_node, WorkerOutput(summary="Security review completed")
    )
    verified = AICodeRuntime._agent_runtime_result_verifier(
        security_node,
        WorkerOutput(
            summary="Security review completed",
            tests_or_checks={
                "check:bandit": {"exit_code": 0},
                "check:ruff": {"exit_code": 0},
                "check:compile": {"exit_code": 0},
            },
        ),
    )
    assert incomplete.accepted is False
    assert verified.accepted is True


@pytest.mark.asyncio
async def test_agent_runtime_builds_tenant_scoped_handlers_with_narrow_tool_allowlists():
    class StubProvider:
        async def complete(self, *, system_prompt, user_prompt):
            return json.dumps({"message": "Analysis complete", "actions": [], "done": True})

    base_worker = SimpleNamespace(
        role=WorkerRole.ANALYST,
        provider=StubProvider(),
        gateway=object(),
        max_steps=4,
        max_actions_per_step=2,
        max_output_chars=1000,
    )
    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.config = SimpleNamespace(
        environment="development",
        tenant_repositories={"tenant-a": ["StrategyG/RusaAi"]},
    )
    runtime.agent_runtime = AgentRuntimeV1(_registry(active=True))
    runtime.worker_id = "worker-queue"
    runtime.queue = SimpleNamespace(owns_lease=lambda *args: True)

    async def build_orchestrator(coding_task, tenant_id):
        assert tenant_id == "tenant-a"
        assert coding_task.repository.full_name == "StrategyG/RusaAi"
        return SimpleNamespace(workers={WorkerRole.ANALYST: base_worker}), "/tmp/agent-runtime-workspace"

    runtime._build_orchestrator = build_orchestrator
    job = _agent_runtime_queue_job({
        "repository": {"full_name": "StrategyG/RusaAi", "base_branch": "main"},
        "agent_runtime_plan": {},
    })
    node = TaskNode(
        task_id="node-scoped-handler",
        selected_role_id="analyst",
        scope="worker",
        objective="Inspect the repository",
        delegated_by_role_id="supreme_orchestrator",
        acceptance_criteria=("Return an analysis",),
        tool_allowlist=("read_file",),
    )
    plan = TaskPlan(
        root_task_id=job.task_id,
        tenant_id=job.tenant_id,
        objective="Run scoped analysis",
        nodes=(node,),
    )

    dispatch = await runtime._build_agent_runtime_dispatch(job, plan, asyncio.Event())
    output = await dispatch["handlers"]["analyst"].execute(node, {})

    assert output.summary == "Analysis complete"
    assert dispatch["coding_task"].repository.full_name == "StrategyG/RusaAi"
    assert dispatch["workspace"] == "/tmp/agent-runtime-workspace"
    escalated = TaskNode(
        **{**node.__dict__, "tool_allowlist": ("write_file",)}
    )
    with pytest.raises(PermissionError, match="exceeds"):
        await dispatch["handlers"]["analyst"].execute(escalated, {})


def test_agent_runtime_queue_payload_is_parsed_with_trusted_job_identity() -> None:
    job = _agent_runtime_queue_job({
        "execution_mode": "agent_runtime_v1",
        "agent_runtime_plan": {
            "schema_version": "1.0",
            "objective": "Execute an approved queue plan",
            "nodes": [{
                "task_id": "node_queue_plan",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the queue task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return evidence"],
                "dependencies": [],
            }],
        },
    })

    plan = AICodeRuntime._agent_runtime_plan_from_job(job)

    assert plan.root_task_id == job.task_id
    assert plan.tenant_id == job.tenant_id
    assert plan.nodes[0].task_id == "node_queue_plan"
    assert plan.nodes[0].dependencies == ()


def test_agent_runtime_queue_payload_rejects_unknown_fields() -> None:
    job = _agent_runtime_queue_job({
        "execution_mode": "agent_runtime_v1",
        "agent_runtime_plan": {
            "objective": "Reject unsupported payload fields",
            "nodes": [{
                "task_id": "node_queue_plan",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the queue task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return evidence"],
                "unreviewed_tool_grant": ["shell"],
            }],
        },
    })

    with pytest.raises(ValueError, match="unsupported fields"):
        AICodeRuntime._agent_runtime_plan_from_job(job)


@pytest.mark.asyncio
async def test_run_once_dispatches_explicit_agent_runtime_queue_job(tmp_path, monkeypatch) -> None:
    job = _agent_runtime_queue_job({
        "execution_mode": "agent_runtime_v1",
        "agent_runtime_plan": {
            "schema_version": "1.0",
            "objective": "Run a queue-integrated Agent Runtime DAG",
            "nodes": [{
                "task_id": "node_queue_dispatch",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the task and return verified evidence",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return verified evidence"],
            }],
        },
    })

    class Queue:
        def __init__(self):
            self.completed = None
            self.approval = None
            self.failed = None

        def claim(self, worker_id, lease_seconds):
            assert worker_id == "worker-queue"
            assert lease_seconds == 180
            return job

        def owns_lease(self, job_id, tenant_id, worker_id, lease_token):
            return (
                job_id == job.id
                and tenant_id == job.tenant_id
                and worker_id == "worker-queue"
                and lease_token == job.lease_token
            )

        def authorize_execution(self, job_id, task_id, tenant_id, worker_id, lease_token):
            return self.owns_lease(job_id, tenant_id, worker_id, lease_token)

        def with_lease_owned(
            self, job_id, tenant_id, worker_id, lease_token, operation, *, lease_seconds
        ):
            assert self.owns_lease(job_id, tenant_id, worker_id, lease_token)
            return operation()

        def complete(self, job_id, tenant_id, worker_id, lease_token, result):
            self.completed = result
            return True

        def await_approval(self, *args):
            self.approval = args
            return True

        def fail(self, *args):
            self.failed = args
            return True

    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.config = SimpleNamespace(
        aicode_enabled=True,
        environment="development",
        tenant_repositories={},
    )
    runtime.worker_id = "worker-queue"
    runtime.agent_runtime = AgentRuntimeV1(_registry(active=True))
    runtime.memory = SQLiteMemory(str(tmp_path / "queue-agent-runtime.sqlite3"))
    runtime.queue = Queue()
    runtime.audit = AuditLog(str(tmp_path / "queue-agent-runtime-audit.jsonl"))
    runtime.configure_agent_runtime_dispatch(
        {"analyst": _Handler()},
        _authorizer,
        _verifier,
    )

    async def idle_heartbeat(claimed_job, lost_lease):
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_heartbeat", idle_heartbeat)
    monkeypatch.setattr(
        "rusa_agents.runtime.record_task_policy",
        lambda *args, **kwargs: None,
    )

    processed = await runtime.run_once()

    assert processed is True
    assert runtime.queue.completed["execution_mode"] == "agent_runtime_v1"
    assert runtime.queue.completed["plan_status"] == "completed"
    assert runtime.queue.approval is None
    assert runtime.queue.failed is None
    checkpoint = runtime.memory.get(job.task_id, tenant_id=job.tenant_id)
    assert checkpoint["state"] == "agent_runtime_checkpoint"
    assert checkpoint["checkpoint_version"] >= 2


@pytest.mark.asyncio
async def test_run_once_publishes_agent_runtime_changes_only_after_ci(tmp_path, monkeypatch):
    job = _agent_runtime_queue_job({
        "execution_mode": "agent_runtime_v1",
        "agent_runtime_plan": {
            "schema_version": "1.0",
            "objective": "Publish a verified Agent Runtime change",
            "nodes": [{
                "task_id": "node_publish",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Analyze the task",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Return a verified analysis"],
            }],
        },
    })

    class Queue:
        def __init__(self):
            self.completed = None
            self.approval = None
            self.failed = None
            self.published = False

        def claim(self, worker_id, lease_seconds):
            return job

        def owns_lease(self, *args):
            return True

        def authorize_execution(self, *args):
            return True

        def publish_if_lease_owned(self, *args, **kwargs):
            publish = args[4]
            self.published = True
            return publish()

        def complete(self, job_id, tenant_id, worker_id, lease_token, result):
            self.completed = result
            return True

        def await_approval(self, *args):
            self.approval = args
            return True

        def fail(self, *args):
            self.failed = args
            return True

    class Publisher:
        def __init__(self):
            self.committed = None
            self.pr_created = False
            self.ci_checked = False

        def commit_and_push(self, workspace, repository, branch, message):
            self.committed = (workspace, repository, branch, message)
            return "commit-agent-runtime"

        async def create_or_get_draft_pr(self, repository, branch, base_branch, title, body):
            self.pr_created = True
            return "https://github.com/StrategyG/RusaAi/pull/999"

        async def wait_for_ci(self, repository, commit_sha, *, timeout_seconds, poll_seconds):
            self.ci_checked = True
            return True, "all required checks passed"

    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.config = SimpleNamespace(
        aicode_enabled=True, environment="development", tenant_repositories={}
    )
    runtime.worker_id = "worker-queue"
    runtime.agent_runtime = AgentRuntimeV1(_registry(active=True))
    runtime.queue = Queue()
    runtime.audit = AuditLog(str(tmp_path / "agent-runtime-publish-audit.jsonl"))
    runtime.publisher = Publisher()
    runtime.workspace_manager = SimpleNamespace(cleanup=lambda task, tenant_id: None)
    coding_task = CodingTask(
        objective="Publish a verified Agent Runtime change",
        repository=RepositoryTarget(
            "StrategyG/RusaAi", "main", "feature/agent-runtime/publish-test"
        ),
        acceptance_criteria=["Return a verified analysis"],
        task_id=job.task_id,
        metadata={"tenant_id": job.tenant_id},
    )
    runtime.configure_agent_runtime_dispatch(
        {"analyst": _Handler()}, _authorizer, _verifier,
        workspace="/tmp/agent-runtime-publish-workspace", coding_task=coding_task,
    )

    async def completed_plan(*args):
        return SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            plan_fingerprint="plan-fingerprint",
            topological_order=("node_publish",),
            node_results=(SimpleNamespace(to_dict=lambda: {"task_id": "node_publish"}),),
        )

    async def idle_heartbeat(claimed_job, lost_lease):
        await asyncio.Event().wait()

    runtime.execute_agent_plan = completed_plan
    monkeypatch.setattr(runtime, "_heartbeat", idle_heartbeat)
    monkeypatch.setattr("rusa_agents.runtime.record_task_policy", lambda *args, **kwargs: None)

    processed = await runtime.run_once()

    assert processed is True
    assert runtime.queue.published is True
    assert runtime.publisher.committed[1:3] == (
        "StrategyG/RusaAi", "feature/agent-runtime/publish-test"
    )
    assert runtime.publisher.pr_created is True
    assert runtime.publisher.ci_checked is True
    assert runtime.queue.completed["commit_sha"] == "commit-agent-runtime"
    assert runtime.queue.completed["pull_request_url"].endswith("/pull/999")
    assert runtime.queue.approval is None
    assert runtime.queue.failed is None


@pytest.mark.asyncio
async def test_run_once_holds_agent_runtime_job_for_review_without_adapters(
    tmp_path, monkeypatch
) -> None:
    job = _agent_runtime_queue_job({
        "execution_mode": "agent_runtime_v1",
        "agent_runtime_plan": {
            "objective": "Keep dispatch disabled until adapters are configured",
            "nodes": [{
                "task_id": "node_queue_hold",
                "selected_role_id": "analyst",
                "scope": "worker",
                "objective": "Wait for reviewed adapters",
                "delegated_by_role_id": "supreme_orchestrator",
                "acceptance_criteria": ["Remain unexecuted"],
            }],
        },
    })

    class Queue:
        def __init__(self):
            self.approval = None

        def claim(self, worker_id, lease_seconds):
            return job

        def owns_lease(self, job_id, tenant_id, worker_id, lease_token):
            return True

        def authorize_execution(self, *args):
            return True

        def await_approval(self, *args):
            self.approval = args
            return True

    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.config = SimpleNamespace(
        aicode_enabled=True,
        environment="development",
        tenant_repositories={},
    )
    runtime.worker_id = "worker-queue"
    runtime.agent_runtime = AgentRuntimeV1(_registry())
    runtime.queue = Queue()
    runtime.audit = AuditLog(str(tmp_path / "queue-agent-runtime-hold-audit.jsonl"))

    async def idle_heartbeat(claimed_job, lost_lease):
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_heartbeat", idle_heartbeat)
    monkeypatch.setattr(
        "rusa_agents.runtime.record_task_policy",
        lambda *args, **kwargs: None,
    )

    processed = await runtime.run_once()

    assert processed is True
    assert runtime.queue.approval is not None
    assert "not configured" in runtime.queue.approval[4]
    assert runtime.agent_runtime.status()["execution_enabled"] is False


def test_aicode_runtime_composes_dag_engine_with_live_queue_lease() -> None:
    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.agent_runtime = AgentRuntimeV1(_registry(active=True))
    runtime.memory = object()
    runtime.queue = object()
    runtime.worker_id = "worker-a"
    job = Job(
        id=7,
        task_id="root-task-001",
        tenant_id="tenant-a",
        payload={},
        attempts=1,
        max_attempts=3,
        lease_owner="worker-a",
        lease_token="00000000-0000-4000-8000-000000000001",
    )
    engine = runtime.build_agent_execution_engine(job, _authorizer, _verifier)
    assert isinstance(engine.checkpoint_store, LeaseBoundCheckpointStore)
    assert engine.checkpoint_store.job_id == job.id
    assert engine.checkpoint_store.task_id == job.task_id
    assert engine.checkpoint_store.tenant_id == job.tenant_id
    assert engine.checkpoint_store.worker_id == runtime.worker_id
    assert engine.checkpoint_store.lease_token == job.lease_token
    with pytest.raises(PermissionError, match="different worker"):
        runtime.build_agent_execution_engine(
            Job(
                id=8,
                task_id="root-task-002",
                tenant_id="tenant-a",
                payload={},
                attempts=1,
                max_attempts=3,
                lease_owner="worker-b",
                lease_token="00000000-0000-4000-8000-000000000002",
            ),
            _authorizer,
            _verifier,
        )


@pytest.mark.asyncio
async def test_worker_engine_executes_dag_and_checkpoints_results() -> None:
    registry = _registry(active=True)
    store = InMemoryCheckpointStore()
    engine = WorkerExecutionEngine(registry, store, _authorizer, _verifier)
    handler_a = _Handler()
    handler_b = _Handler()
    plan = _plan(
        _node("task-a", "analyst"),
        _node("task-b", "developer", ("task-a",)),
    )
    result = await engine.execute(plan, {"analyst": handler_a, "developer": handler_b}, trusted_tenant_id="tenant-a")
    assert result.status == PlanStatus.COMPLETED
    assert [item.status for item in result.node_results] == [
        NodeStatus.COMPLETED, NodeStatus.COMPLETED
    ]
    assert handler_a.calls == 1
    assert handler_b.calls == 1
    checkpoint = store.load(plan.root_task_id, plan.tenant_id)
    assert checkpoint["plan_fingerprint"] == result.plan_fingerprint




@pytest.mark.asyncio
async def test_worker_engine_rejects_expired_deadline_before_starting_worker() -> None:
    engine = WorkerExecutionEngine(
        _registry(active=True), InMemoryCheckpointStore(), _authorizer, _verifier
    )
    handler = _Handler()
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    result = await engine.execute(
        _plan(_node("task-expired", "analyst", deadline_at=expired)),
        {"analyst": handler},
        trusted_tenant_id="tenant-a",
    )
    assert result.status == PlanStatus.FAILED
    assert result.node_results[0].status == NodeStatus.FAILED
    assert "deadline expired" in (result.node_results[0].error or "").lower()
    assert handler.calls == 0


@pytest.mark.asyncio
async def test_worker_engine_rejects_model_supplied_tenant_mismatch() -> None:
    store = InMemoryCheckpointStore()
    handler = _Handler()
    policy_calls = []

    def authorizer(node, tenant_id):
        policy_calls.append(tenant_id)
        return PolicyDecision(allowed=True)

    engine = WorkerExecutionEngine(_registry(active=True), store, authorizer, _verifier)
    plan = _plan(_node("task-tenant", "analyst"))
    with pytest.raises(PermissionError, match="authenticated tenant context"):
        await engine.execute(
            plan,
            {"analyst": handler},
            trusted_tenant_id="tenant-b",
        )
    assert handler.calls == 0
    assert policy_calls == []
    assert store.load(plan.root_task_id, "tenant-a") is None


@pytest.mark.asyncio
async def test_worker_engine_fails_closed_for_draft_registry() -> None:
    registry = _registry()
    engine = WorkerExecutionEngine(
        registry, InMemoryCheckpointStore(), _authorizer, _verifier
    )
    with pytest.raises(RegistryNotActivatedError, match="disabled"):
        await engine.execute(
            _plan(_node("task-a", "analyst")),
            {"analyst": _Handler()}, trusted_tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_worker_engine_requires_approval_for_high_risk_nodes() -> None:
    engine = WorkerExecutionEngine(
        _registry(active=True), InMemoryCheckpointStore(), _authorizer, _verifier
    )
    handler = _Handler()
    result = await engine.execute(
        _plan(_node("task-a", "analyst", risk_class="high")),
        {"analyst": handler}, trusted_tenant_id="tenant-a")
    assert result.status == PlanStatus.NEEDS_APPROVAL
    assert result.node_results[0].status == NodeStatus.NEEDS_APPROVAL
    assert handler.calls == 0


@pytest.mark.asyncio
async def test_worker_engine_denies_policy_and_blocks_dependants() -> None:
    def deny(node, tenant_id):
        return PolicyDecision(allowed=False, reason="denied by test policy")

    engine = WorkerExecutionEngine(
        _registry(active=True), InMemoryCheckpointStore(), deny, _verifier
    )
    handler_a = _Handler()
    handler_b = _Handler()
    result = await engine.execute(
        _plan(
            _node("task-a", "analyst"),
            _node("task-b", "developer", ("task-a",)),
        ),
        {"analyst": handler_a, "developer": handler_b}, trusted_tenant_id="tenant-a")
    assert result.status == PlanStatus.BLOCKED
    assert result.node_results[0].status == NodeStatus.BLOCKED
    assert result.node_results[1].status == NodeStatus.BLOCKED
    assert handler_a.calls == handler_b.calls == 0


@pytest.mark.asyncio
async def test_worker_engine_retries_and_resumes_completed_nodes() -> None:
    store = InMemoryCheckpointStore()
    engine = WorkerExecutionEngine(_registry(active=True), store, _authorizer, _verifier)
    handler = _Handler(fail=True)
    plan = _plan(_node("task-a", "analyst"))
    failed = await engine.execute(plan, {"analyst": handler}, trusted_tenant_id="tenant-a")
    assert failed.status == PlanStatus.FAILED
    assert handler.calls == 2
    replacement = _Handler()
    resumed = await engine.execute(plan, {"analyst": replacement}, trusted_tenant_id="tenant-a")
    assert resumed.status == PlanStatus.COMPLETED
    assert replacement.calls == 1
    replay = await engine.execute(plan, {"analyst": _Handler()}, trusted_tenant_id="tenant-a")
    assert replay.status == PlanStatus.COMPLETED
    assert replay.node_results[0].attempts == 1



@pytest.mark.asyncio
async def test_worker_engine_reports_failure_when_dependants_are_blocked() -> None:
    engine = WorkerExecutionEngine(
        _registry(active=True), InMemoryCheckpointStore(), _authorizer, _verifier
    )
    failing = _Handler(fail=True)
    dependent = _Handler()
    result = await engine.execute(
        _plan(
            _node("task-failing", "analyst"),
            _node("task-dependent", "developer", ("task-failing",)),
        ),
        {"analyst": failing, "developer": dependent},
        trusted_tenant_id="tenant-a",
    )
    assert result.status == PlanStatus.FAILED
    assert result.node_results[0].status == NodeStatus.FAILED
    assert result.node_results[1].status == NodeStatus.BLOCKED
    assert failing.calls == 2
    assert dependent.calls == 0


@pytest.mark.asyncio
async def test_worker_engine_rejects_checkpoint_for_different_plan() -> None:
    store = InMemoryCheckpointStore()
    engine = WorkerExecutionEngine(_registry(active=True), store, _authorizer, _verifier)
    plan = _plan(_node("task-a", "analyst"))
    await engine.execute(plan, {"analyst": _Handler()}, trusted_tenant_id="tenant-a")
    changed = _plan(_node("task-a", "analyst", risk_class="medium"))
    with pytest.raises(ValueError, match="fingerprint"):
        await engine.execute(changed, {"analyst": _Handler()}, trusted_tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_worker_engine_persists_checkpoints_through_existing_task_memory(tmp_path) -> None:
    memory = SQLiteMemory(str(tmp_path / "agent-runtime-memory.sqlite3"))
    store = TaskMemoryCheckpointStore(memory)
    engine = WorkerExecutionEngine(_registry(active=True), store, _authorizer, _verifier)
    plan = _plan(_node("task-memory", "analyst"))
    result = await engine.execute(
        plan,
        {"analyst": _Handler()},
        trusted_tenant_id="tenant-a",
    )
    assert result.status == PlanStatus.COMPLETED
    record = memory.get(plan.root_task_id, tenant_id="tenant-a")
    assert record["state"] == "agent_runtime_checkpoint"
    assert record["payload"]["plan_fingerprint"] == result.plan_fingerprint
    assert memory.get(plan.root_task_id, tenant_id="tenant-b") is None


@pytest.mark.asyncio
async def test_worker_engine_cancels_in_flight_node_without_stale_checkpoint() -> None:
    cancellation = asyncio.Event()
    store = InMemoryCheckpointStore()
    engine = WorkerExecutionEngine(_registry(active=True), store, _authorizer, _verifier)

    class CancellationAwareHandler:
        def __init__(self):
            self.started = False
            self.cancelled = False

        async def execute(self, node, dependency_results):
            self.started = True
            cancellation.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    handler = CancellationAwareHandler()
    plan = _plan(_node("task-cancel", "analyst"))
    result = await engine.execute(
        plan,
        {"analyst": handler},
        trusted_tenant_id="tenant-a",
        cancellation_event=cancellation,
    )

    assert result.status == PlanStatus.CANCELLED
    assert result.node_results[0].status == NodeStatus.CANCELLED
    assert handler.started is True
    assert handler.cancelled is True
    # Queue cancellation revokes the lease; the worker must not write a stale checkpoint.
    assert store.load(plan.root_task_id, plan.tenant_id) is None


@pytest.mark.asyncio
async def test_queue_lease_bound_engine_uses_atomic_checkpoint_cas(tmp_path) -> None:
    class StubLeaseQueue:
        def __init__(self):
            self.calls = 0
            self.valid = True

        def owns_lease(self, job_id, tenant_id, worker_id, lease_token):
            return self.valid

        def with_lease_owned(
            self, job_id, tenant_id, worker_id, lease_token, operation, *, lease_seconds
        ):
            self.calls += 1
            if not self.valid:
                raise RuntimeError("lease revoked")
            return operation()

    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.agent_runtime = AgentRuntimeV1(_registry(active=True))
    runtime.memory = SQLiteMemory(str(tmp_path / "queue-dag-cas.sqlite3"))
    runtime.queue = StubLeaseQueue()
    runtime.worker_id = "worker-cas"
    job = Job(
        id=77,
        task_id="root-task-001",
        tenant_id="tenant-a",
        payload={},
        attempts=1,
        max_attempts=3,
        lease_owner="worker-cas",
        lease_token="00000000-0000-4000-8000-000000000077",
    )
    plan = _plan(_node("task-queue-cas", "analyst"))
    result = await runtime.execute_agent_plan(
        job,
        plan,
        {"analyst": _Handler()},
        _authorizer,
        _verifier,
        asyncio.Event(),
    )

    assert result.status == PlanStatus.COMPLETED
    assert runtime.queue.calls >= 3  # fenced load, node checkpoint, final checkpoint
    record = runtime.memory.get(plan.root_task_id, tenant_id="tenant-a")
    assert record["state"] == "agent_runtime_checkpoint"
    assert record["checkpoint_version"] >= 2


@pytest.mark.asyncio
async def test_worker_runtime_propagates_queue_lease_loss_to_dag_cancellation(tmp_path) -> None:
    cancellation = asyncio.Event()

    class StubLeaseQueue:
        def __init__(self):
            self.valid = True
            self.checkpoint_calls = 0

        def owns_lease(self, job_id, tenant_id, worker_id, lease_token):
            return self.valid

        def with_lease_owned(
            self, job_id, tenant_id, worker_id, lease_token, operation, *, lease_seconds
        ):
            self.checkpoint_calls += 1
            if not self.valid:
                raise RuntimeError("lease revoked")
            return operation()

    runtime = AICodeRuntime.__new__(AICodeRuntime)
    runtime.agent_runtime = AgentRuntimeV1(_registry(active=True))
    runtime.memory = SQLiteMemory(str(tmp_path / "queue-cancel.sqlite3"))
    runtime.queue = StubLeaseQueue()
    runtime.worker_id = "worker-cancel"
    job = Job(
        id=88,
        task_id="root-task-001",
        tenant_id="tenant-a",
        payload={},
        attempts=1,
        max_attempts=3,
        lease_owner="worker-cancel",
        lease_token="00000000-0000-4000-8000-000000000088",
    )

    class LeaseCancellingHandler:
        def __init__(self):
            self.cancelled = False

        async def execute(self, node, dependency_results):
            cancellation.set()
            runtime.queue.valid = False
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    handler = LeaseCancellingHandler()
    plan = _plan(_node("task-queue-cancel", "analyst"))
    result = await runtime.execute_agent_plan(
        job,
        plan,
        {"analyst": handler},
        _authorizer,
        _verifier,
        cancellation,
    )

    assert result.status == PlanStatus.CANCELLED
    assert result.node_results[0].status == NodeStatus.CANCELLED
    assert handler.cancelled is True
    assert runtime.memory.get(plan.root_task_id, tenant_id="tenant-a") is None
    assert runtime.queue.checkpoint_calls == 1  # only the initial lease-fenced read


@pytest.mark.asyncio
async def test_high_risk_node_executes_only_after_policy_confirms_approval() -> None:
    def approved(node, tenant_id):
        return PolicyDecision(allowed=True, requires_approval=True, approval_satisfied=True)

    engine = WorkerExecutionEngine(
        _registry(active=True), InMemoryCheckpointStore(), approved, _verifier
    )
    handler = _Handler()
    result = await engine.execute(
        _plan(_node("task-approved", "analyst", risk_class="high")),
        {"analyst": handler}, trusted_tenant_id="tenant-a")
    assert result.status == PlanStatus.COMPLETED
    assert handler.calls == 1
