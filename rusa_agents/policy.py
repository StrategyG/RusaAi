"""Fail-closed, allowlist-based autonomy policy."""

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
    DEFAULT_ALLOWED_ACTIONS = frozenset(
        {
            "repo_read",
            "branch_create",
            "workspace_write",
            "test_run",
            "security_scan",
            "commit_feature_branch",
            "pull_request_create",
            "audit_read",
        }
    )
    PROTECTED_ACTIONS = frozenset(
        {
            "merge",
            "deploy",
            "release",
            "secret_change",
            "permission_change",
            "destructive_action",
            "production_write",
        }
    )
    DENIED_ACTIONS = frozenset(
        {
            "disable_audit",
            "exfiltrate_secret",
            "force_push_main",
            "delete_protected_branch",
        }
    )

    def __init__(self, allowed_actions: set[str] | None = None) -> None:
        # An explicitly empty allowlist must stay empty; it must not fall back to defaults.
        selected = (
            self.DEFAULT_ALLOWED_ACTIONS
            if allowed_actions is None
            else frozenset(allowed_actions)
        )
        self.allowed_actions = frozenset(selected)

    def evaluate(
        self,
        action: str,
        risk: RiskLevel = RiskLevel.LOW,
    ) -> PolicyDecision:
        if action in self.DENIED_ACTIONS:
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reason=f"Action '{action}' is prohibited",
            )
        if action in self.PROTECTED_ACTIONS:
            return PolicyDecision(
                allowed=False,
                requires_approval=True,
                reason=f"Human approval is required for '{action}'",
            )
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(
                allowed=False,
                requires_approval=True,
                reason=f"{risk.value} risk requires human approval",
            )
        if action not in self.allowed_actions:
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reason=f"Action '{action}' is not allowlisted",
            )
        return PolicyDecision(
            allowed=True,
            requires_approval=False,
            reason="Allowed by autonomy policy",
        )
