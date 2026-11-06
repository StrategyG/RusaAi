"""Policy-enforced tool registry; no arbitrary model-generated shell execution."""
from dataclasses import dataclass
from typing import Any, Callable

from .policy import AutonomyPolicy, RiskLevel


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    action: str
    risk: RiskLevel = RiskLevel.LOW


@dataclass(frozen=True)
class ToolResult:
    tool_id: str
    success: bool
    output: Any = None
    error: str | None = None


class ToolRegistry:
    def __init__(self, policy: AutonomyPolicy | None = None) -> None:
        self.policy = policy or AutonomyPolicy()
        self._tools: dict[str, tuple[ToolSpec, Callable[..., Any]]] = {}

    def register(self, spec: ToolSpec, handler: Callable[..., Any]) -> None:
        if not spec.tool_id.strip():
            raise ValueError("tool_id is required")
        if spec.tool_id in self._tools:
            raise ValueError(f"tool already registered: {spec.tool_id}")
        self._tools[spec.tool_id] = (spec, handler)

    def execute(self, tool_id: str, *, approval_granted: bool = False, **kwargs: Any) -> ToolResult:
        if tool_id not in self._tools:
            raise LookupError(f"unknown tool: {tool_id}")
        spec, handler = self._tools[tool_id]
        decision = self.policy.evaluate(
            spec.action,
            spec.risk,
            approval_granted=approval_granted,
        )
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return ToolResult(tool_id=tool_id, success=True, output=handler(**kwargs))
