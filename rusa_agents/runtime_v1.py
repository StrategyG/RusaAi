"""Composition entry point for the staged Agent Runtime v1.0."""
from __future__ import annotations

import json
from typing import Any, Mapping

from .capability_registry import AgentWorkerRegistry, RegistryNotActivatedError
from .capability_router import CapabilityRouter, RouteDecision, RouteRequest
from .task_decomposer import TaskDecomposer, TaskPlan, TaskPlanValidation
from .worker_execution import CheckpointStore, PolicyAuthorizer, ResultVerifier, WorkerExecutionEngine


class AgentRuntimeV1:
    """Read-only registry preview plus approval-gated task execution composition."""

    def __init__(self, registry: AgentWorkerRegistry | None = None) -> None:
        self.registry = registry or AgentWorkerRegistry.load()
        self.router = CapabilityRouter(self.registry)
        self.decomposer = TaskDecomposer(self.registry)

    def status(self) -> dict[str, Any]:
        return self.registry.summary()

    def preview_route(self, request: RouteRequest) -> RouteDecision:
        return self.router.route(request)

    def validate_plan(self, plan: TaskPlan) -> TaskPlanValidation:
        return self.decomposer.validate(plan)

    def build_execution_engine(
        self,
        checkpoint_store: CheckpointStore,
        policy_authorizer: PolicyAuthorizer,
        result_verifier: ResultVerifier,
        *,
        worker_parallelism: int = 1,
    ) -> WorkerExecutionEngine:
        if self.registry.status != "active":
            raise RegistryNotActivatedError(
                "The draft registry can be inspected and routed in preview mode only"
            )
        return WorkerExecutionEngine(
            registry=self.registry,
            checkpoint_store=checkpoint_store,
            policy_authorizer=policy_authorizer,
            result_verifier=result_verifier,
            worker_parallelism=worker_parallelism,
        )


def main() -> None:
    runtime = AgentRuntimeV1()
    print(json.dumps(runtime.status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
