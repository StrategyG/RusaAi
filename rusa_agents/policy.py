"""Allowlist-based autonomy policy with explicit approval boundaries."""

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
    requires_approval: bool
    reason: str


class AutonomyPolicy:
    DEFAULT_ALLOWED = frozenset(
        {
            "repo.read",
            "repo.branch.create",
            "repo.file.write",
            "repo.pull_request.create",
            "ci.status.read",
        }
    )
    PROTECTED_ACTIONS = frozenset(
        {
            "repo.merge",
            "repo.delete",
            "repo.force_push",
            "deploy",
            "release",
            "secret.change",
            "permission.change",
            "production.write",
            "workflow.change",
        }
    )
    DENIED_ACTIONS = frozenset(
        {
            "audit.disable",
            "secret.exfiltrate",
            "main.force_push",
            "repository.transfer",
        }
    )

    def __init__(
        self,
        allowed_actions: set[str] | frozenset[str] | None = None,
    ) -> None:
        # None means defaults; an explicitly empty set must deny every action.
        self.allowed_actions = frozenset(
            self.DEFAULT_ALLOWED if allowed_actions is None else allowed_actions
        )

    def evaluate(self, action: str, risk: RiskLevel = RiskLevel.LOW) -> PolicyDecision:
        normalized = action.strip().lower()
        if normalized in self.DENIED_ACTIONS:
            return PolicyDecision(False, False, f"Action '{normalized}' is prohibited")
        if normalized in self.PROTECTED_ACTIONS:
            return PolicyDecision(
                False,
                True,
                f"Human approval is required for protected action '{normalized}'",
            )
        if normalized not in self.allowed_actions:
            return PolicyDecision(False, False, f"Action '{normalized}' is not allowlisted")
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(False, True, f"{risk.value} risk requires human approval")
        return PolicyDecision(True, False, "Allowed by autonomy policy")
