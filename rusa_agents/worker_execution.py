"""Sequential, checkpointed DAG worker executor with fail-closed policy gates."""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol

from .capability_registry import AgentWorkerRegistry, RegistryNotActivatedError
from .task_decomposer import TaskDecomposer, TaskNode, TaskPlan, TaskPlanValidation


class NodeStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_APPROVAL = "needs_approval"
    CANCELLED = "cancelled"


class PlanStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class WorkerOutput:
    summary: str
    success: bool = True
    artifacts: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    tests_or_checks: Mapping[str, Any] = field(default_factory=dict)
    known_limitations: tuple[str, ...] = ()
    handoff_summary: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    approval_satisfied: bool = False
    reason: str = ""


@dataclass(frozen=True)
class VerificationDecision:
    accepted: bool
    reason: str = ""


@dataclass(frozen=True)
class NodeExecutionResult:
    task_id: str
    role_id: str
    status: NodeStatus
    attempts: int = 0
    summary: str = ""
    artifacts: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    tests_or_checks: Mapping[str, Any] = field(default_factory=dict)
    known_limitations: tuple[str, ...] = ()
    handoff_summary: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> NodeExecutionResult:
        return cls(
            task_id=str(value["task_id"]),
            role_id=str(value["role_id"]),
            status=NodeStatus(str(value["status"])),
            attempts=int(value.get("attempts", 0)),
            summary=str(value.get("summary", "")),
            artifacts=tuple(value.get("artifacts", ())),
            evidence_refs=tuple(value.get("evidence_refs", ())),
            tests_or_checks=dict(value.get("tests_or_checks", {})),
            known_limitations=tuple(value.get("known_limitations", ())),
            handoff_summary=str(value.get("handoff_summary", "")),
            error=value.get("error"),
        )


@dataclass(frozen=True)
class TaskExecutionResult:
    root_task_id: str
    tenant_id: str
    status: PlanStatus
    node_results: tuple[NodeExecutionResult, ...]
    topological_order: tuple[str, ...]
    plan_fingerprint: str


class WorkerHandler(Protocol):
    async def execute(
        self, node: TaskNode, dependency_results: Mapping[str, NodeExecutionResult]
    ) -> WorkerOutput: ...


class CheckpointStore(Protocol):
    def load(self, task_id: str, tenant_id: str) -> Mapping[str, Any] | None: ...

    def save(self, task_id: str, tenant_id: str, payload: Mapping[str, Any]) -> None: ...


class InMemoryCheckpointStore:
    """Test/development checkpoint store. Production must use durable task memory."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def load(self, task_id: str, tenant_id: str) -> Mapping[str, Any] | None:
        value = self._items.get((tenant_id, task_id))
        return json.loads(json.dumps(value)) if value is not None else None

    def save(self, task_id: str, tenant_id: str, payload: Mapping[str, Any]) -> None:
        self._items[(tenant_id, task_id)] = json.loads(json.dumps(payload))


class TaskMemoryCheckpointStore:
    """Adapter over the existing SQLiteMemory/PostgresMemory task memory contract."""

    def __init__(self, memory: Any, repository_label: str = "agent-runtime") -> None:
        self.memory = memory
        self.repository_label = repository_label

    def load(self, task_id: str, tenant_id: str) -> Mapping[str, Any] | None:
        record = self.memory.get(task_id, tenant_id=tenant_id)
        return record.get("payload") if record else None

    def save(self, task_id: str, tenant_id: str, payload: Mapping[str, Any]) -> None:
        self.memory.save(
            task_id,
            self.repository_label,
            "agent_runtime_checkpoint",
            dict(payload),
            tenant_id=tenant_id,
        )


PolicyAuthorizer = Callable[[TaskNode, str], PolicyDecision | Awaitable[PolicyDecision]]
ResultVerifier = Callable[
    [TaskNode, WorkerOutput], VerificationDecision | Awaitable[VerificationDecision]
]


class WorkerExecutionEngine:
    """Runs a validated DAG one worker at a time and checkpoints every node.

    This is the Control Plane worker scheduler, not an inference server. Model
    calls must still pass through the configured Rusaai Model Access Gateway,
    which enforces the single active generation limit for the CPU profile.
    """

    def __init__(
        self,
        registry: AgentWorkerRegistry,
        checkpoint_store: CheckpointStore,
        policy_authorizer: PolicyAuthorizer,
        result_verifier: ResultVerifier,
        worker_parallelism: int = 1,
    ) -> None:
        if worker_parallelism != 1:
            raise ValueError("CPU-first Agent Runtime v1.0 requires worker_parallelism=1")
        self.registry = registry
        self.decomposer = TaskDecomposer(registry)
        self.checkpoint_store = checkpoint_store
        self.policy_authorizer = policy_authorizer
        self.result_verifier = result_verifier

    @staticmethod
    async def _resolve(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _fingerprint(plan: TaskPlan) -> str:
        payload = {
            "root_task_id": plan.root_task_id,
            "tenant_id": plan.tenant_id,
            "objective": plan.objective,
            "nodes": [
                {
                    **asdict(node),
                    "acceptance_criteria": list(node.acceptance_criteria),
                    "dependencies": list(node.dependencies),
                    "tool_allowlist": list(node.tool_allowlist),
                    "requested_permissions": list(node.requested_permissions),
                    "context_refs": list(node.context_refs),
                }
                for node in plan.nodes
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(canonical.encode("utf-8")).hexdigest()

    async def _save_checkpoint(
        self,
        plan: TaskPlan,
        fingerprint: str,
        results: Mapping[str, NodeExecutionResult],
        status: str,
    ) -> None:
        payload = {
            "schema_version": "1.0",
            "plan_fingerprint": fingerprint,
            "tenant_id": plan.tenant_id,
            "plan_status": status,
            "node_results": [result.to_dict() for result in results.values()],
        }
        await asyncio.to_thread(
            self.checkpoint_store.save, plan.root_task_id, plan.tenant_id, payload
        )

    async def execute(
        self,
        plan: TaskPlan,
        handlers: Mapping[str, WorkerHandler],
        *,
        resume: bool = True,
    ) -> TaskExecutionResult:
        validation: TaskPlanValidation = self.decomposer.validate(plan)
        if self.registry.status != "active":
            raise RegistryNotActivatedError(
                "Agent Runtime execution is disabled until the registry is approved and activated"
            )
        for node in plan.nodes:
            self.registry.require_dispatchable(node.selected_role_id)
            if node.selected_role_id not in handlers:
                raise ValueError(f"No worker handler is registered for {node.selected_role_id}")

        fingerprint = self._fingerprint(plan)
        checkpoint = (
            await asyncio.to_thread(
                self.checkpoint_store.load, plan.root_task_id, plan.tenant_id
            )
            if resume else None
        )
        if checkpoint:
            if checkpoint.get("tenant_id") != plan.tenant_id:
                raise ValueError("Checkpoint tenant does not match the task tenant")
            if checkpoint.get("plan_fingerprint") != fingerprint:
                raise ValueError("Checkpoint plan fingerprint does not match the submitted plan")

        results: dict[str, NodeExecutionResult] = {}
        if checkpoint:
            for item in checkpoint.get("node_results", []):
                result = NodeExecutionResult.from_dict(item)
                if result.task_id not in {node.task_id for node in plan.nodes}:
                    raise ValueError("Checkpoint contains an unknown task node")
                results[result.task_id] = result

        for task_id in validation.topological_order:
            if task_id in results and results[task_id].status == NodeStatus.COMPLETED:
                continue
            node = next(item for item in plan.nodes if item.task_id == task_id)
            dependency_ids = validation.effective_dependencies[task_id]
            failed_dependencies = [
                dependency_id for dependency_id in dependency_ids
                if dependency_id not in results
                or results[dependency_id].status != NodeStatus.COMPLETED
            ]
            if failed_dependencies:
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.BLOCKED,
                    error="Dependencies are not completed: " + ", ".join(failed_dependencies),
                )
                await self._save_checkpoint(plan, fingerprint, results, "blocked")
                continue

            decision = await self._resolve(
                self.policy_authorizer(node, plan.tenant_id)
            )
            if not isinstance(decision, PolicyDecision):
                raise TypeError("policy_authorizer must return PolicyDecision")
            if decision.requires_approval or (
                node.approval_required and not decision.approval_satisfied
            ):
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.NEEDS_APPROVAL,
                    error=decision.reason or "Human approval is required before execution",
                )
                await self._save_checkpoint(plan, fingerprint, results, "needs_approval")
                continue
            if not decision.allowed:
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.BLOCKED,
                    error=decision.reason or "Policy Engine denied this task",
                )
                await self._save_checkpoint(plan, fingerprint, results, "blocked")
                continue

            dependency_results = {key: results[key] for key in dependency_ids}
            last_error = None
            completed_result: NodeExecutionResult | None = None
            for attempt in range(1, node.retry_limit + 2):
                try:
                    output = await handlers[node.selected_role_id].execute(
                        node, dependency_results
                    )
                    if not isinstance(output, WorkerOutput):
                        raise TypeError("Worker handler must return WorkerOutput")
                    verification = await self._resolve(
                        self.result_verifier(node, output)
                    )
                    if not isinstance(verification, VerificationDecision):
                        raise TypeError("result_verifier must return VerificationDecision")
                    if not output.success:
                        raise RuntimeError(
                            "; ".join(output.known_limitations) or "Worker reported failure"
                        )
                    if not verification.accepted:
                        raise RuntimeError(
                            verification.reason or "Independent result verification failed"
                        )
                    completed_result = NodeExecutionResult(
                        task_id=task_id,
                        role_id=node.selected_role_id,
                        status=NodeStatus.COMPLETED,
                        attempts=attempt,
                        summary=output.summary,
                        artifacts=output.artifacts,
                        evidence_refs=output.evidence_refs,
                        tests_or_checks=dict(output.tests_or_checks),
                        known_limitations=output.known_limitations,
                        handoff_summary=output.handoff_summary or output.summary,
                    )
                    break
                except asyncio.CancelledError:
                    results[task_id] = NodeExecutionResult(
                        task_id=task_id,
                        role_id=node.selected_role_id,
                        status=NodeStatus.CANCELLED,
                        attempts=attempt,
                        error="Execution was cancelled",
                    )
                    await self._save_checkpoint(plan, fingerprint, results, "cancelled")
                    raise
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
            results[task_id] = completed_result or NodeExecutionResult(
                task_id=task_id,
                role_id=node.selected_role_id,
                status=NodeStatus.FAILED,
                attempts=node.retry_limit + 1,
                error=last_error or "Worker execution failed",
            )
            await self._save_checkpoint(
                plan,
                fingerprint,
                results,
                results[task_id].status.value,
            )

        ordered_results = tuple(results[task_id] for task_id in validation.topological_order)
        statuses = {item.status for item in ordered_results}
        if statuses == {NodeStatus.COMPLETED}:
            plan_status = PlanStatus.COMPLETED
        elif NodeStatus.NEEDS_APPROVAL in statuses:
            plan_status = PlanStatus.NEEDS_APPROVAL
        elif NodeStatus.BLOCKED in statuses:
            plan_status = PlanStatus.BLOCKED
        else:
            plan_status = PlanStatus.FAILED
        await self._save_checkpoint(plan, fingerprint, results, plan_status.value)
        return TaskExecutionResult(
            root_task_id=plan.root_task_id,
            tenant_id=plan.tenant_id,
            status=plan_status,
            node_results=ordered_results,
            topological_order=validation.topological_order,
            plan_fingerprint=fingerprint,
        )


class AICodeWorkerAdapter:
    """Adapts an existing AICode Worker to the generic DAG handler contract."""

    def __init__(self, worker: Any, repository: Any, tenant_id: str, context: str = "") -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.worker = worker
        self.repository = repository
        self.tenant_id = tenant_id
        self.context = context

    async def execute(
        self, node: TaskNode, dependency_results: Mapping[str, NodeExecutionResult]
    ) -> WorkerOutput:
        from .contracts import CodingTask, WorkerResult, WorkerRole

        WorkerRole(node.selected_role_id)
        prior_results = []
        for result in dependency_results.values():
            try:
                prior_role = WorkerRole(result.role_id)
            except ValueError:
                continue
            prior_results.append(
                WorkerResult(
                    role=prior_role,
                    summary=result.handoff_summary or result.summary,
                    success=result.status == NodeStatus.COMPLETED,
                    artifacts=list(result.artifacts),
                    evidence=dict(result.tests_or_checks),
                    blockers=list(result.known_limitations),
                )
            )
        context = self.context
        if dependency_results:
            handoffs = [
                f"{item.role_id}: {item.handoff_summary or item.summary}"
                for item in dependency_results.values()
            ]
            context = (context + "\n" + "\n".join(handoffs)).strip()
        task = CodingTask(
            objective=node.objective,
            repository=self.repository,
            acceptance_criteria=list(node.acceptance_criteria),
            context=context,
            task_id=node.task_id,
            metadata={"tenant_id": self.tenant_id},
        )
        result = await self.worker.run(task, prior_results)
        return WorkerOutput(
            summary=result.summary,
            success=result.success and not result.blockers,
            artifacts=tuple(result.artifacts),
            evidence_refs=tuple(result.evidence.keys()),
            tests_or_checks=dict(result.evidence),
            known_limitations=tuple(result.blockers),
            handoff_summary=result.summary,
        )
