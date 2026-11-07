"""Risk-aware autonomy policy with protected production operations."""
from __future__ import annotations

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
    PROTECTED_ACTIONS = {
        "merge", "deploy", "release", "secret_change",
        "permission_change", "destructive_action", "production_write",
    }
    DENIED_ACTIONS = {
        "disable_audit", "exfiltrate_secret", "force_push_main",
    }

    def __init__(self, allowed_actions: set[str] | None = None):
        defaults = {
            "repo_read", "branch_create", "workspace_write",
            "test_run", "commit_feature_branch", "pull_request_create", "audit_read",
        }
        # None means use the safe built-in defaults. An explicitly empty set
        # is an intentional deny-all policy and must never be widened.
        self.allowed_actions = set(defaults if allowed_actions is None else allowed_actions)

    def evaluate(
        self, action: str, risk: RiskLevel = RiskLevel.LOW
    ) -> PolicyDecision:
        if action in self.DENIED_ACTIONS:
            return PolicyDecision(False, False, f"Action '{action}' is prohibited")
        if action in self.PROTECTED_ACTIONS:
            return PolicyDecision(False, True, f"Human approval required for '{action}'")
        if action not in self.allowed_actions:
            return PolicyDecision(False, False, f"Action '{action}' is not allowlisted")
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(False, True, f"{risk.value} risk requires human approval")
        return PolicyDecision(True, False, "Allowed by autonomy policy")
