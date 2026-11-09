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
        if allowed_actions is not None and (
            not isinstance(allowed_actions, set)
            or any(not isinstance(item, str) or not item.strip() for item in allowed_actions)
        ):
            raise ValueError("allowed_actions must be a set of non-empty action names")
        self.allowed_actions = frozenset(defaults if allowed_actions is None else allowed_actions)

    def evaluate(
        self, action: str, risk: RiskLevel = RiskLevel.LOW
    ) -> PolicyDecision:
        if not isinstance(action, str) or not action.strip():
            return PolicyDecision(False, False, "Action name is invalid")
        if not isinstance(risk, RiskLevel):
            try:
                risk = RiskLevel(risk)
            except (TypeError, ValueError):
                return PolicyDecision(False, False, "Risk level is invalid")
        if action in self.DENIED_ACTIONS:
            return PolicyDecision(False, False, f"Action '{action}' is prohibited")
        if action in self.PROTECTED_ACTIONS:
            return PolicyDecision(False, True, f"Human approval required for '{action}'")
        if action not in self.allowed_actions:
            return PolicyDecision(False, False, f"Action '{action}' is not allowlisted")
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(False, True, f"{risk.value} risk requires human approval")
        return PolicyDecision(True, False, "Allowed by autonomy policy")
