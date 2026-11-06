"""Fail-closed, risk-aware autonomy policy."""
from dataclasses import dataclass
from enum import StrEnum

class RiskLevel(StrEnum):
    LOW="low"; MEDIUM="medium"; HIGH="high"; CRITICAL="critical"

@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str

class AutonomyPolicy:
    PROTECTED_ACTIONS=frozenset({"merge","deploy","release","secret_change","permission_change","destructive_action","production_write","network_access","dependency_install"})
    DENIED_ACTIONS=frozenset({"disable_audit","exfiltrate_secret","force_push_main","read_host_secrets","privileged_execution"})
    DEFAULT_ALLOWED=frozenset({"repo_read","workspace_write","test_run","lint_run","build_run","audit_read","artifact_write"})
    def __init__(self, allowed_actions=None):
        self.allowed_actions=frozenset(self.DEFAULT_ALLOWED if allowed_actions is None else allowed_actions)
    def evaluate(self, action: str, risk: RiskLevel=RiskLevel.LOW) -> PolicyDecision:
        if action in self.DENIED_ACTIONS:
            return PolicyDecision(False,False,f"Action '{action}' is prohibited")
        if action in self.PROTECTED_ACTIONS:
            return PolicyDecision(False,True,f"Human approval required for '{action}'")
        if risk in {RiskLevel.HIGH,RiskLevel.CRITICAL}:
            return PolicyDecision(False,True,f"{risk.value} risk requires human approval")
        if action not in self.allowed_actions:
            return PolicyDecision(False,False,f"Action '{action}' is not allowlisted")
        return PolicyDecision(True,False,"Allowed by autonomy policy")
