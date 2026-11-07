"""Immutable QA contract tests for worker role and protected-path enforcement."""
import pytest

from rusa_agents.contracts import WorkerRole
from rusa_agents.tool_gateway import ToolDenied, WorkspaceToolGateway


@pytest.mark.parametrize("role", [WorkerRole.TESTER, WorkerRole.SECURITY])
def test_quality_roles_cannot_write_workspace(tmp_path, role):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.write_file("src/unauthorized.py", "VALUE = True", role)


@pytest.mark.parametrize(
    "path",
    [
        "tests/qa_contract/test_policy_contract.py",
        "rusa_agents/policy.py",
        "rusa_agents/orchestrator.py",
        "rusa_agents/tool_gateway.py",
        "rusa_agents/git_workspace.py",
        "rusa_agents/runtime.py",
        "rusa_agents/config.py",
        "rusa_agents/qa_service.py",
        "rusa_agents/qa_client.py",
        "rusa_agents/qa_attestation.py",
        "Dockerfile.worker",
        "Dockerfile.agent-sandbox",
        "docker-compose.production.yml",
        "scripts/deploy/compose-deploy.sh",
    ],
)
def test_worker_cannot_modify_qa_and_release_enforcement_paths(tmp_path, path):
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied):
        gateway.write_file(path, "MALICIOUS = True", WorkerRole.DEVELOPER)
