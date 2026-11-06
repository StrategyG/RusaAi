"""Fail-closed autonomy policy for agent and tool actions."""
from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    approval_required: bool
    reason: str


class AutonomyPolicy:
    DEFAULT_ALLOWLIST = frozenset(
        {
            "agent.execute",
            "repository.read",
            "research.search",
            "document.read",
            "artifact.create",
            "workspace.write",
            "test.run",
            "branch.create",
            "feature_branch.commit",
            "pull_request.create",
            "memory.read",
            "memory.write",
        }
    )
    APPROVAL_ACTIONS = frozenset(
        {
            "repository.merge",
            "deployment.production",
            "release.publish",
            "secret.change",
            "permission.change",
            "data.destructive",
            "production.write",
        }
    )
    DENY_ACTIONS = frozenset(
        {
            "audit.disable",
            "secret.exfiltrate",
            "protected_branch.force_push",
            "policy.self_modify",
        }
    )

    def __init__(self, allowlist: frozenset[str] | None = None) -> None:
        self.allowlist = allowlist if allowlist is not None else self.DEFAULT_ALLOWLIST

    def evaluate(
        self,
        action: str,
        risk: RiskLevel = RiskLevel.LOW,
        *,
        approval_granted: bool = False,
    ) -> PolicyDecision:
        if action in self.DENY_ACTIONS:
            return PolicyDecision(False, False, f"Action '{action}' is prohibited")
        if action in self.APPROVAL_ACTIONS:
            if approval_granted:
                return PolicyDecision(True, False, "Explicit approval supplied")
            return PolicyDecision(
                False, True, f"Human approval required for '{action}'"
            )
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(
                False, True, f"{risk.value} risk requires human approval"
            )
        if action not in self.allowlist:
            return PolicyDecision(False, False, f"Action '{action}' is not allowlisted")
        return PolicyDecision(True, False, "Allowed by autonomy policy")
