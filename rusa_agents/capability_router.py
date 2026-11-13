"""Deterministic capability-ID routing; routing never grants tool permissions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .capability_registry import AgentWorkerRegistry


@dataclass(frozen=True)
class RouteRequest:
    scope: Literal["agent", "worker"]
    required_capability_ids: tuple[str, ...] = ()
    explicit_role_id: str | None = None
    identity_type: str | None = None
    parent_role_id: str | None = None
    max_candidates: int = 5
    require_all_capabilities: bool = True


@dataclass(frozen=True)
class RouteCandidate:
    role_id: str
    display_name: str
    identity_type: str
    score: float
    matched_capability_ids: tuple[str, ...]
    dispatchable: bool
    explanation: str


@dataclass(frozen=True)
class RouteDecision:
    status: Literal["ROUTED", "PREVIEW_ONLY", "NO_MATCH"]
    selected_role_id: str | None
    suggested_role_id: str | None
    candidates: tuple[RouteCandidate, ...]
    tool_grants: tuple[str, ...] = ()


class CapabilityRouter:
    """Routes only on explicit role IDs or source-linked capability chunk IDs.

    It intentionally does not infer a semantic match from unapproved chunk
    text. Tool permissions remain the responsibility of Policy Engine/Tool Gateway.
    """

    def __init__(self, registry: AgentWorkerRegistry) -> None:
        self.registry = registry

    def route(self, request: RouteRequest) -> RouteDecision:
        if request.scope not in {"agent", "worker"}:
            raise ValueError("scope must be 'agent' or 'worker'")
        if not 1 <= request.max_candidates <= 10:
            raise ValueError("max_candidates must be between 1 and 10")
        capability_ids = tuple(dict.fromkeys(request.required_capability_ids))
        if not request.explicit_role_id and not capability_ids:
            raise ValueError("explicit_role_id or required_capability_ids is required")

        known_ids = {
            chunk_id
            for role in self.registry.list_roles()
            for chunk_id in role.capability_chunk_ids
        }
        unknown = sorted(set(capability_ids) - known_ids)
        if unknown:
            raise ValueError("Unknown capability chunk IDs: " + ", ".join(unknown))

        candidates: list[RouteCandidate] = []
        for role in self.registry.list_roles(request.scope):
            if request.identity_type and role.identity_type != request.identity_type:
                continue
            if request.parent_role_id is not None and role.parent_role_id != request.parent_role_id:
                continue
            if request.explicit_role_id and role.role_id != request.explicit_role_id:
                continue

            matched = tuple(
                chunk_id for chunk_id in capability_ids
                if chunk_id in role.capability_chunk_ids
            )
            if capability_ids and request.require_all_capabilities and len(matched) != len(capability_ids):
                continue
            if not request.explicit_role_id and not matched:
                continue
            score = (
                1000.0 if request.explicit_role_id
                else round(len(matched) / max(len(capability_ids), 1), 4)
            )
            dispatchable = self.registry.is_dispatchable(role)
            explanation = (
                "Explicit role match."
                if request.explicit_role_id
                else f"Matched {len(matched)} of {len(capability_ids)} requested capability IDs."
            )
            if not dispatchable:
                explanation += " Preview only: registry/role is not approved and active."
            candidates.append(
                RouteCandidate(
                    role_id=role.role_id,
                    display_name=role.display_name,
                    identity_type=role.identity_type,
                    score=score,
                    matched_capability_ids=matched,
                    dispatchable=dispatchable,
                    explanation=explanation,
                )
            )

        candidates.sort(key=lambda item: (-item.score, item.role_id))
        candidates = candidates[: request.max_candidates]
        if not candidates:
            return RouteDecision("NO_MATCH", None, None, (), ())
        winner = candidates[0]
        if winner.dispatchable:
            return RouteDecision("ROUTED", winner.role_id, winner.role_id, tuple(candidates), ())
        return RouteDecision("PREVIEW_ONLY", None, winner.role_id, tuple(candidates), ())
