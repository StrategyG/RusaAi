"""Sequential, checkpointed DAG worker executor with fail-closed policy gates."""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from threading import RLock
from typing import Any, Protocol

from .capability_registry import AgentWorkerRegistry, RegistryNotActivatedError
from .queue import LeaseLostError
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
    CANCELLED = "cancelled"


class CheckpointConflictError(RuntimeError):
    """A checkpoint changed after the caller read its expected version."""


class ExecutionCancellationRequested(RuntimeError):
    """The owning queue lease or task cancellation signal was revoked."""


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

    def compare_and_set(
        self,
        task_id: str,
        tenant_id: str,
        payload: Mapping[str, Any],
        expected_version: int,
    ) -> int: ...


class InMemoryCheckpointStore:
    """Thread-safe test/development checkpoint store with versioned writes."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = RLock()

    def load(self, task_id: str, tenant_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            value = self._items.get((tenant_id, task_id))
            return json.loads(json.dumps(value)) if value is not None else None

    def compare_and_set(
        self,
        task_id: str,
        tenant_id: str,
        payload: Mapping[str, Any],
        expected_version: int,
    ) -> int:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        with self._lock:
            current = self._items.get((tenant_id, task_id))
            current_version = int(current.get("checkpoint_version", 0)) if current else 0
            if current_version != expected_version:
                raise CheckpointConflictError(
                    f"Checkpoint version conflict: expected {expected_version}, found {current_version}"
                )
            next_version = expected_version + 1
            updated = dict(payload)
            updated["checkpoint_version"] = next_version
            self._items[(tenant_id, task_id)] = json.loads(json.dumps(updated))
            return next_version

    def save(self, task_id: str, tenant_id: str, payload: Mapping[str, Any]) -> None:
        expected_version = int(payload.get("checkpoint_version", 0))
        self.compare_and_set(task_id, tenant_id, payload, expected_version)


class TaskMemoryCheckpointStore:
    """Atomic versioned adapter over SQLiteMemory/PostgresMemory task memory."""

    STATE = "agent_runtime_checkpoint"

    def __init__(self, memory: Any, repository_label: str = "agent-runtime") -> None:
        self.memory = memory
        self.repository_label = repository_label

    def load(self, task_id: str, tenant_id: str) -> Mapping[str, Any] | None:
        record = self.memory.get(task_id, tenant_id=tenant_id)
        if not record or record.get("state") != self.STATE:
            return None
        payload = dict(record.get("payload") or {})
        payload["checkpoint_version"] = int(record.get("checkpoint_version", 0))
        return payload

    def compare_and_set(
        self,
        task_id: str,
        tenant_id: str,
        payload: Mapping[str, Any],
        expected_version: int,
    ) -> int:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        updated = dict(payload)
        updated["tenant_id"] = tenant_id
        updated["checkpoint_version"] = expected_version + 1
        version = self.memory.compare_and_set(
            task_id,
            self.repository_label,
            self.STATE,
            updated,
            tenant_id,
            expected_version,
        )
        if version is None:
            raise CheckpointConflictError(
                f"Checkpoint compare-and-set failed for task {task_id!r} "
                f"at expected version {expected_version}"
            )
        return int(version)

    def save(self, task_id: str, tenant_id: str, payload: Mapping[str, Any]) -> None:
        expected_version = int(payload.get("checkpoint_version", 0))
        self.compare_and_set(task_id, tenant_id, payload, expected_version)


class LeaseBoundCheckpointStore:
    """Fence checkpoint reads/writes with the existing PostgreSQL task lease."""

    def __init__(
        self,
        checkpoint_store: CheckpointStore,
        queue: Any,
        job_id: int,
        task_id: str,
        tenant_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 180,
    ) -> None:
        self.checkpoint_store = checkpoint_store
        self.queue = queue
        self.job_id = job_id
        self.task_id = task_id
        self.tenant_id = tenant_id
        self.worker_id = worker_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds

    def _run_with_lease(
        self, task_id: str, tenant_id: str, operation: Callable[[], Any]
    ) -> Any:
        if task_id != self.task_id:
            raise PermissionError("Checkpoint task does not match the active queue job")
        if tenant_id != self.tenant_id:
            raise PermissionError("Checkpoint tenant does not match the active queue lease")
        return self.queue.with_lease_owned(
            self.job_id,
            self.tenant_id,
            self.worker_id,
            self.lease_token,
            operation,
            lease_seconds=self.lease_seconds,
        )

    def load(self, task_id: str, tenant_id: str) -> Mapping[str, Any] | None:
        return self._run_with_lease(
            task_id, tenant_id, lambda: self.checkpoint_store.load(task_id, tenant_id)
        )

    def compare_and_set(
        self,
        task_id: str,
        tenant_id: str,
        payload: Mapping[str, Any],
        expected_version: int,
    ) -> int:
        return self._run_with_lease(
            task_id,
            tenant_id,
            lambda: self.checkpoint_store.compare_and_set(
                task_id, tenant_id, payload, expected_version
            ),
        )

    def save(self, task_id: str, tenant_id: str, payload: Mapping[str, Any]) -> None:
        expected_version = int(payload.get("checkpoint_version", 0))
        self.compare_and_set(task_id, tenant_id, payload, expected_version)


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
    def _deadline_remaining_seconds(node: TaskNode) -> float | None:
        if not node.deadline_at:
            return None
        deadline = datetime.fromisoformat(node.deadline_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError(f"{node.task_id} deadline_at must include a timezone")
        return (deadline - datetime.now(timezone.utc)).total_seconds()

    async def _resolve_before_deadline(self, value: Any, node: TaskNode) -> Any:
        remaining = self._deadline_remaining_seconds(node)
        if remaining is None:
            return await self._resolve(value)
        if remaining <= 0:
            raise TimeoutError("Task node deadline expired")
        return await asyncio.wait_for(self._resolve(value), timeout=remaining)

    async def _resolve_or_cancel(
        self,
        value: Any,
        node: TaskNode,
        cancellation_event: asyncio.Event | None,
    ) -> Any:
        if cancellation_event is None:
            return await self._resolve_before_deadline(value, node)
        operation = asyncio.create_task(self._resolve_before_deadline(value, node))
        cancellation = asyncio.create_task(cancellation_event.wait())
        try:
            await asyncio.wait(
                {operation, cancellation},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_event.is_set():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise ExecutionCancellationRequested(
                    "Task cancellation or queue lease revocation was observed"
                )
            return await operation
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            if not cancellation.done():
                cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)

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
        expected_version: int,
    ) -> int:
        payload = {
            "schema_version": "1.0",
            "plan_fingerprint": fingerprint,
            "tenant_id": plan.tenant_id,
            "plan_status": status,
            "node_results": [result.to_dict() for result in results.values()],
        }
        return await asyncio.to_thread(
            self.checkpoint_store.compare_and_set,
            plan.root_task_id,
            plan.tenant_id,
            payload,
            expected_version,
        )

    async def execute(
        self,
        plan: TaskPlan,
        handlers: Mapping[str, WorkerHandler],
        *,
        trusted_tenant_id: str,
        resume: bool = True,
        cancellation_event: asyncio.Event | None = None,
    ) -> TaskExecutionResult:
        if not isinstance(trusted_tenant_id, str) or not trusted_tenant_id.strip():
            raise ValueError("trusted_tenant_id is required")
        if plan.tenant_id != trusted_tenant_id:
            raise PermissionError(
                "Task plan tenant does not match authenticated tenant context"
            )
        validation: TaskPlanValidation = self.decomposer.validate(plan)
        if self.registry.status != "active":
            raise RegistryNotActivatedError(
                "Agent Runtime execution is disabled until the registry is approved and activated"
            )
        for node in plan.nodes:
            self.registry.require_dispatchable(node.selected_role_id)
            self.registry.require_dispatchable(node.delegated_by_role_id)
            if node.selected_role_id not in handlers:
                raise ValueError(f"No worker handler is registered for {node.selected_role_id}")
            handler_tenant_id = getattr(handlers[node.selected_role_id], "tenant_id", trusted_tenant_id)
            if handler_tenant_id != trusted_tenant_id:
                raise PermissionError(
                    f"Worker handler tenant does not match authenticated tenant context: {node.selected_role_id}"
                )

        fingerprint = self._fingerprint(plan)
        checkpoint = (
            await asyncio.to_thread(
                self.checkpoint_store.load, plan.root_task_id, plan.tenant_id
            )
            if resume else None
        )
        checkpoint_version = int(checkpoint.get("checkpoint_version", 0)) if checkpoint else 0
        if checkpoint_version < 0:
            raise ValueError("Checkpoint version cannot be negative")
        if checkpoint:
            if checkpoint.get("tenant_id") != plan.tenant_id:
                raise ValueError("Checkpoint tenant does not match the task tenant")
            if checkpoint.get("plan_fingerprint") != fingerprint:
                raise ValueError("Checkpoint plan fingerprint does not match the submitted plan")

        results: dict[str, NodeExecutionResult] = {}
        node_map = {node.task_id: node for node in plan.nodes}
        if checkpoint:
            for item in checkpoint.get("node_results", []):
                result = NodeExecutionResult.from_dict(item)
                if result.task_id not in node_map:
                    raise ValueError("Checkpoint contains an unknown task node")
                results[result.task_id] = result

        cancelled = False
        for task_id in validation.topological_order:
            if cancellation_event is not None and cancellation_event.is_set():
                cancelled = True
                break
            if task_id in results and results[task_id].status == NodeStatus.COMPLETED:
                continue
            node = node_map[task_id]
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
                checkpoint_version = await self._save_checkpoint(
                    plan, fingerprint, results, "blocked", checkpoint_version
                )
                continue

            try:
                decision = await self._resolve_or_cancel(
                    self.policy_authorizer(node, trusted_tenant_id),
                    node,
                    cancellation_event,
                )
            except ExecutionCancellationRequested:
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.CANCELLED,
                    error="Execution was cancelled before policy authorization completed",
                )
                cancelled = True
                break
            except asyncio.CancelledError:
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.CANCELLED,
                    error="Execution coroutine was cancelled during policy authorization",
                )
                try:
                    checkpoint_version = await self._save_checkpoint(
                        plan, fingerprint, results, "cancelled", checkpoint_version
                    )
                except LeaseLostError:
                    pass
                raise
            except TimeoutError:
                remaining = self._deadline_remaining_seconds(node)
                if remaining is None or remaining > 0:
                    raise
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.FAILED,
                    error="Task node deadline expired while awaiting policy authorization",
                )
                checkpoint_version = await self._save_checkpoint(
                    plan, fingerprint, results, "failed", checkpoint_version
                )
                continue
            if not isinstance(decision, PolicyDecision):
                raise TypeError("policy_authorizer must return PolicyDecision")
            approval_required = decision.requires_approval or node.approval_required
            if approval_required and not decision.approval_satisfied:
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.NEEDS_APPROVAL,
                    error=decision.reason or "Human approval is required before execution",
                )
                checkpoint_version = await self._save_checkpoint(
                    plan, fingerprint, results, "needs_approval", checkpoint_version
                )
                continue
            if not decision.allowed:
                results[task_id] = NodeExecutionResult(
                    task_id=task_id,
                    role_id=node.selected_role_id,
                    status=NodeStatus.BLOCKED,
                    error=decision.reason or "Policy Engine denied this task",
                )
                checkpoint_version = await self._save_checkpoint(
                    plan, fingerprint, results, "blocked", checkpoint_version
                )
                continue

            dependency_results = {key: results[key] for key in dependency_ids}
            last_error = None
            completed_result: NodeExecutionResult | None = None
            attempts = 0
            for attempt in range(1, node.retry_limit + 2):
                attempts = attempt
                try:
                    output = await self._resolve_or_cancel(
                        handlers[node.selected_role_id].execute(node, dependency_results),
                        node,
                        cancellation_event,
                    )
                    if not isinstance(output, WorkerOutput):
                        raise TypeError("Worker handler must return WorkerOutput")
                    verification = await self._resolve_or_cancel(
                        self.result_verifier(node, output),
                        node,
                        cancellation_event,
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
                except ExecutionCancellationRequested:
                    results[task_id] = NodeExecutionResult(
                        task_id=task_id,
                        role_id=node.selected_role_id,
                        status=NodeStatus.CANCELLED,
                        attempts=attempt,
                        error="Execution was cancelled or its queue lease was revoked",
                    )
                    cancelled = True
                    break
                except asyncio.CancelledError:
                    results[task_id] = NodeExecutionResult(
                        task_id=task_id,
                        role_id=node.selected_role_id,
                        status=NodeStatus.CANCELLED,
                        attempts=attempt,
                        error="Execution coroutine was cancelled",
                    )
                    try:
                        checkpoint_version = await self._save_checkpoint(
                            plan, fingerprint, results, "cancelled", checkpoint_version
                        )
                    except LeaseLostError:
                        # A revoked/expired lease must never be bypassed to persist a stale checkpoint.
                        pass
                    raise
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                    remaining = self._deadline_remaining_seconds(node)
                    if remaining is not None and remaining <= 0:
                        break
            if cancelled:
                break
            results[task_id] = completed_result or NodeExecutionResult(
                task_id=task_id,
                role_id=node.selected_role_id,
                status=NodeStatus.FAILED,
                attempts=attempts,
                error=last_error or "Worker execution failed",
            )
            checkpoint_version = await self._save_checkpoint(
                plan,
                fingerprint,
                results,
                results[task_id].status.value,
                checkpoint_version,
            )

        if cancellation_event is not None and cancellation_event.is_set():
            cancelled = True
        if cancelled:
            for task_id in validation.topological_order:
                if task_id not in results:
                    node = node_map[task_id]
                    results[task_id] = NodeExecutionResult(
                        task_id=task_id,
                        role_id=node.selected_role_id,
                        status=NodeStatus.CANCELLED,
                        error="Plan was cancelled before this node started",
                    )

        ordered_results = tuple(results[task_id] for task_id in validation.topological_order)
        statuses = {item.status for item in ordered_results}
        if cancelled or NodeStatus.CANCELLED in statuses:
            plan_status = PlanStatus.CANCELLED
        elif statuses == {NodeStatus.COMPLETED}:
            plan_status = PlanStatus.COMPLETED
        elif NodeStatus.NEEDS_APPROVAL in statuses:
            plan_status = PlanStatus.NEEDS_APPROVAL
        elif NodeStatus.FAILED in statuses:
            plan_status = PlanStatus.FAILED
        elif NodeStatus.BLOCKED in statuses:
            plan_status = PlanStatus.BLOCKED
        else:
            plan_status = PlanStatus.FAILED
        if not cancelled:
            checkpoint_version = await self._save_checkpoint(
                plan, fingerprint, results, plan_status.value, checkpoint_version
            )
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
