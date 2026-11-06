"""Fail-closed tool authorization and human approval policy."""
from __future__ import annotations

from dataclasses import dataclass


POLICY_VERSION = "rusa-autonomy-policy-v1"


PROTECTED_TOOLS = {
    "github.merge_pull_request",
    "github.deploy",
    "github.release",
    "github.change_secret",
    "github.change_permission",
    "shell.execute",
    "filesystem.delete",
    "database.migrate",
    "audit.disable",
}
NEVER_ALLOWED_TOOLS = {
    "audit.disable",
    "secret.exfiltrate",
    "github.force_push_main",
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


class AutonomyPolicy:
    def evaluate(
        self,
        tool_name: str,
        allowed_tools: set[str],
        approved_actions: set[str],
    ) -> PolicyDecision:
        if tool_name in NEVER_ALLOWED_TOOLS:
            return PolicyDecision(False, False, "Tool is prohibited by platform policy")
        if tool_name not in allowed_tools:
            return PolicyDecision(False, False, "Tool is not allowlisted for this task")
        if tool_name in PROTECTED_TOOLS:
            if tool_name in approved_actions:
                return PolicyDecision(True, False, "Human approval recorded for this task")
            return PolicyDecision(False, True, "Human approval is required for this action")
        return PolicyDecision(True, False, "Allowed by task-scoped tool policy")
