"""Validated task-plan and DAG contracts for Agent/Worker execution."""
from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Mapping

from .capability_registry import AgentWorkerRegistry


_ROLE_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_TOOL_ID = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_RISK_CLASSES = {"low", "medium", "high", "critical"}
_TARGET_ENVIRONMENTS = {"development", "staging", "production"}


@dataclass(frozen=True)
class TaskNode:
    task_id: str
    selected_role_id: str
    scope: str
    objective: str
    delegated_by_role_id: str
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    parent_task_id: str | None = None
    workspace_ref: str | None = None
    deadline_at: str | None = None
    retry_limit: int = 1
    tool_allowlist: tuple[str, ...] = ()
    requested_permissions: tuple[str, ...] = ()
    risk_class: str = "low"
    context_refs: tuple[str, ...] = ()
    change_id: str | None = None
    target_environment: str | None = None
    release_or_change_version: str | None = None
    lifecycle_gate: str | None = None
    rollback_stop_condition: str | None = None

    @property
    def approval_required(self) -> bool:
        return self.risk_class in {"high", "critical"} or self.target_environment == "production"


@dataclass(frozen=True)
class TaskPlan:
    root_task_id: str
    tenant_id: str
    objective: str
    nodes: tuple[TaskNode, ...]


@dataclass(frozen=True)
class TaskPlanValidation:
    topological_order: tuple[str, ...]
    effective_dependencies: Mapping[str, tuple[str, ...]]
    approval_required_node_ids: tuple[str, ...]


class TaskDecomposer:
    """Validates planner output before any worker is selected or executed."""

    def __init__(self, registry: AgentWorkerRegistry, max_nodes: int = 64) -> None:
        if not 1 <= max_nodes <= 256:
            raise ValueError("max_nodes must be between 1 and 256")
        self.registry = registry
        self.max_nodes = max_nodes

    def validate(self, plan: TaskPlan) -> TaskPlanValidation:
        if not plan.root_task_id.strip() or not plan.tenant_id.strip() or not plan.objective.strip():
            raise ValueError("root_task_id, tenant_id, and objective are required")
        if not plan.nodes or len(plan.nodes) > self.max_nodes:
            raise ValueError(f"Task plan must contain between 1 and {self.max_nodes} nodes")

        ids = [node.task_id for node in plan.nodes]
        if any(not _ROLE_ID.fullmatch(node.selected_role_id) for node in plan.nodes):
            raise ValueError("Task node contains an invalid selected_role_id")
        if any(not node.task_id.strip() for node in plan.nodes):
            raise ValueError("Task node task_id cannot be blank")
        if plan.root_task_id in ids or len(ids) != len(set(ids)):
            raise ValueError("Task IDs must be unique and must not equal root_task_id")
        node_map = {node.task_id: node for node in plan.nodes}
        dependencies: dict[str, set[str]] = {}
        children: dict[str, set[str]] = defaultdict(set)
        indegree = {task_id: 0 for task_id in ids}
        approval_required: list[str] = []

        for node in plan.nodes:
            if not node.objective.strip() or not node.acceptance_criteria:
                raise ValueError(f"{node.task_id} requires an objective and acceptance criteria")
            if any(not item.strip() for item in node.acceptance_criteria):
                raise ValueError(f"{node.task_id} has a blank acceptance criterion")
            if node.scope not in {"agent", "worker"}:
                raise ValueError(f"{node.task_id} scope must be 'agent' or 'worker'")
            if not 0 <= node.retry_limit <= 5:
                raise ValueError(f"{node.task_id} retry_limit must be between 0 and 5")
            if node.risk_class not in _RISK_CLASSES:
                raise ValueError(f"{node.task_id} has an unsupported risk_class")
            if len(set(node.tool_allowlist)) != len(node.tool_allowlist):
                raise ValueError(f"{node.task_id} tool_allowlist contains duplicates")
            if len(set(node.requested_permissions)) != len(node.requested_permissions):
                raise ValueError(f"{node.task_id} requested_permissions contains duplicates")
            if any(not _TOOL_ID.fullmatch(value) or value == "*" for value in (
                *node.tool_allowlist, *node.requested_permissions
            )):
                raise ValueError(f"{node.task_id} contains an invalid tool or permission identifier")
            if node.deadline_at:
                try:
                    deadline = datetime.fromisoformat(node.deadline_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"{node.task_id} deadline_at must be ISO-8601") from exc
                if deadline.tzinfo is None:
                    raise ValueError(f"{node.task_id} deadline_at must include a timezone")

            role = self.registry.get_role(node.selected_role_id)
            if role.scope != node.scope:
                raise ValueError(
                    f"{node.task_id} scope does not match role {node.selected_role_id}"
                )
            delegator = self.registry.get_role(node.delegated_by_role_id)
            if delegator.identity_type != "core_agent":
                raise ValueError(f"{node.task_id} must be delegated by a core agent")
            if role.identity_type == "devops_lifecycle_profile":
                if role.parent_role_id != "devops" or node.delegated_by_role_id != "devops":
                    raise ValueError(
                        f"{node.task_id} DevOps lifecycle profile must be delegated by devops"
                    )
                required = (
                    node.change_id,
                    node.target_environment,
                    node.release_or_change_version,
                    node.lifecycle_gate,
                    node.rollback_stop_condition,
                )
                if any(not value or not value.strip() for value in required):
                    raise ValueError(
                        f"{node.task_id} DevOps profile requires change, environment, "
                        "version, lifecycle gate, and rollback/stop condition"
                    )
                if node.target_environment not in _TARGET_ENVIRONMENTS:
                    raise ValueError(f"{node.task_id} has an unsupported target_environment")
            elif any((
                node.change_id, node.target_environment, node.release_or_change_version,
                node.lifecycle_gate, node.rollback_stop_condition,
            )):
                if node.target_environment not in _TARGET_ENVIRONMENTS:
                    raise ValueError(f"{node.task_id} has an unsupported target_environment")

            parent_id = node.parent_task_id or plan.root_task_id
            if parent_id == node.task_id or parent_id not in node_map and parent_id != plan.root_task_id:
                raise ValueError(f"{node.task_id} has an invalid parent_task_id")
            node_dependencies = set(node.dependencies)
            if len(node_dependencies) != len(node.dependencies):
                raise ValueError(f"{node.task_id} contains duplicate dependencies")
            if node.task_id in node_dependencies or not node_dependencies.issubset(node_map):
                raise ValueError(f"{node.task_id} contains an unknown or self dependency")
            if parent_id != plan.root_task_id:
                node_dependencies.add(parent_id)
            dependencies[node.task_id] = node_dependencies
            if node.approval_required:
                approval_required.append(node.task_id)

        for child_id, parents in dependencies.items():
            for parent_id in parents:
                children[parent_id].add(child_id)
                indegree[child_id] += 1

        ready = deque(node.task_id for node in plan.nodes if indegree[node.task_id] == 0)
        ordered: list[str] = []
        while ready:
            task_id = ready.popleft()
            ordered.append(task_id)
            for child_id in sorted(children[task_id]):
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
        if len(ordered) != len(plan.nodes):
            raise ValueError("Task dependencies or parent relationships contain a cycle")

        # Parent ancestry must terminate at the plan root; dependencies alone do not imply ownership.
        for node in plan.nodes:
            current = node
            seen: set[str] = set()
            while current.parent_task_id and current.parent_task_id != plan.root_task_id:
                if current.parent_task_id in seen:
                    raise ValueError("Task parent hierarchy contains a cycle")
                seen.add(current.parent_task_id)
                current = node_map[current.parent_task_id]
            if current.parent_task_id not in {None, plan.root_task_id}:
                raise ValueError(f"{node.task_id} is not attached to the plan root")

        return TaskPlanValidation(
            topological_order=tuple(ordered),
            effective_dependencies={
                task_id: tuple(sorted(values)) for task_id, values in dependencies.items()
            },
            approval_required_node_ids=tuple(sorted(approval_required)),
        )
