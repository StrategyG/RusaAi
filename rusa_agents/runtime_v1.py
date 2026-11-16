"""Composition entry point for the staged Agent Runtime v1.0."""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .capability_registry import (
    AgentWorkerRegistry,
    RegistryNotActivatedError,
    RegistryValidationError,
)
from .capability_router import CapabilityRouter, RouteDecision, RouteRequest
from .task_decomposer import TaskDecomposer, TaskPlan, TaskPlanValidation
from .worker_execution import (
    CheckpointStore,
    PolicyAuthorizer,
    ResultVerifier,
    WorkerExecutionEngine,
)


class AgentRuntimeV1:
    """Read-only registry preview plus approval-gated task execution composition."""

    def __init__(self, registry: AgentWorkerRegistry | None = None) -> None:
        self.registry = registry or AgentWorkerRegistry.load()
        self.router = CapabilityRouter(self.registry)
        self.decomposer = TaskDecomposer(self.registry)

    def status(self) -> dict[str, Any]:
        return self.registry.summary()

    def validate_aicode_worker_bindings(
        self, worker_role_ids: Iterable[str]
    ) -> dict[str, Any]:
        """Verify that the legacy AICode pipeline maps to registered v1 worker roles.

        This compatibility check does not activate roles or grant capabilities.
        """
        role_ids = tuple(worker_role_ids)
        if not role_ids or len(role_ids) != len(set(role_ids)):
            raise ValueError("AICode worker role IDs must be non-empty and unique")
        validated: list[str] = []
        for role_id in role_ids:
            role = self.registry.get_role(role_id)
            if role.identity_type != "aicode_worker" or role.scope != "worker":
                raise RegistryValidationError(
                    f"AICode pipeline role {role_id!r} is not registered as an AICode worker"
                )
            validated.append(role_id)
        return {
            "compatible": True,
            "worker_role_ids": list(validated),
            "dispatchable": all(self.registry.is_dispatchable(role_id) for role_id in validated),
        }

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
