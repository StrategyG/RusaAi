import pytest

from rusa_agents.agent_tools import ROLE_CHECK_PROFILES, ROLE_TOOL_ALLOWLIST
from rusa_agents.autonomous import AutonomousCodeWorker
from rusa_agents.contracts import WorkerRole
from rusa_agents.tool_gateway import ToolDenied, ToolResult, WorkspaceToolGateway


def test_role_tool_registry_is_explicit_and_least_privilege():
    assert set(ROLE_TOOL_ALLOWLIST) == set(WorkerRole)
    assert "write_file" in ROLE_TOOL_ALLOWLIST[WorkerRole.DEVELOPER]
    assert "write_file" not in ROLE_TOOL_ALLOWLIST[WorkerRole.SECURITY]
    assert "run_check" not in ROLE_TOOL_ALLOWLIST[WorkerRole.ANALYST]


def test_validation_profiles_are_role_scoped():
    assert ROLE_CHECK_PROFILES[WorkerRole.TESTER] == {"pytest"}
    assert "bandit" in ROLE_CHECK_PROFILES[WorkerRole.SECURITY]
    assert "bandit" not in ROLE_CHECK_PROFILES[WorkerRole.DEBUGGER]


def test_runtime_worker_uses_the_shared_role_registry():
    assert AutonomousCodeWorker.ROLE_TOOLS == ROLE_TOOL_ALLOWLIST


class FakeSandbox:
    def run(self, workspace, profile):
        return ToolResult(True, "passed", {"profile": profile, "exit_code": 0})


def test_gateway_uses_shared_validation_profile_registry(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path, sandbox=FakeSandbox())
    assert gateway.run_check("pytest", WorkerRole.TESTER).ok
    with pytest.raises(ToolDenied, match="cannot run"):
        gateway.run_check("bandit", WorkerRole.TESTER)


def test_gateway_rejects_direct_call_outside_role_allowlist(tmp_path):
    gateway = WorkspaceToolGateway(tmp_path, sandbox=FakeSandbox())
    action = {"name": "write_file", "arguments": {"path": "src/app.py", "content": "pass\n"}}
    with pytest.raises(ToolDenied, match="not allowed"):
        gateway.execute(action, WorkerRole.SECURITY)
