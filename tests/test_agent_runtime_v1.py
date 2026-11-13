"""Tests for the staged Agent Runtime v1.0 registry, router, DAG and executor."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from rusa_agents.capability_registry import (
    AgentWorkerRegistry,
    RegistryNotActivatedError,
    RegistryValidationError,
)
from rusa_agents.capability_router import CapabilityRouter, RouteRequest
from rusa_agents.task_decomposer import TaskDecomposer, TaskNode, TaskPlan
from rusa_agents.worker_execution import (
    InMemoryCheckpointStore,
    NodeStatus,
    PlanStatus,
    PolicyDecision,
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
        for role in data["roles"]:
            role["registration_status"] = "registered"
            role["runtime_status"] = "verified"
            role["activation_state"] = "active"
    return AgentWorkerRegistry.from_mapping(data, schema)


def _node(
    task_id: str,
    role_id: str,
    dependencies: tuple[str, ...] = (),
    *,
    risk_class: str = "low",
    parent_task_id: str | None = None,
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
    result = await engine.execute(plan, {"analyst": handler_a, "developer": handler_b})
    assert result.status == PlanStatus.COMPLETED
    assert [item.status for item in result.node_results] == [
        NodeStatus.COMPLETED, NodeStatus.COMPLETED
    ]
    assert handler_a.calls == 1
    assert handler_b.calls == 1
    checkpoint = store.load(plan.root_task_id, plan.tenant_id)
    assert checkpoint["plan_fingerprint"] == result.plan_fingerprint


@pytest.mark.asyncio
async def test_worker_engine_fails_closed_for_draft_registry() -> None:
    registry = _registry()
    engine = WorkerExecutionEngine(
        registry, InMemoryCheckpointStore(), _authorizer, _verifier
    )
    with pytest.raises(RegistryNotActivatedError, match="disabled"):
        await engine.execute(
            _plan(_node("task-a", "analyst")),
            {"analyst": _Handler()},
        )


@pytest.mark.asyncio
async def test_worker_engine_requires_approval_for_high_risk_nodes() -> None:
    engine = WorkerExecutionEngine(
        _registry(active=True), InMemoryCheckpointStore(), _authorizer, _verifier
    )
    handler = _Handler()
    result = await engine.execute(
        _plan(_node("task-a", "analyst", risk_class="high")),
        {"analyst": handler},
    )
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
        {"analyst": handler_a, "developer": handler_b},
    )
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
    failed = await engine.execute(plan, {"analyst": handler})
    assert failed.status == PlanStatus.FAILED
    assert handler.calls == 2
    replacement = _Handler()
    resumed = await engine.execute(plan, {"analyst": replacement})
    assert resumed.status == PlanStatus.COMPLETED
    assert replacement.calls == 1
    replay = await engine.execute(plan, {"analyst": _Handler()})
    assert replay.status == PlanStatus.COMPLETED
    assert replay.node_results[0].attempts == 1


@pytest.mark.asyncio
async def test_worker_engine_rejects_checkpoint_for_different_plan() -> None:
    store = InMemoryCheckpointStore()
    engine = WorkerExecutionEngine(_registry(active=True), store, _authorizer, _verifier)
    plan = _plan(_node("task-a", "analyst"))
    await engine.execute(plan, {"analyst": _Handler()})
    changed = _plan(_node("task-a", "analyst", risk_class="medium"))
    with pytest.raises(ValueError, match="fingerprint"):
        await engine.execute(changed, {"analyst": _Handler()})
