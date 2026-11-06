import pytest

from rusa_agents.policy import AutonomyPolicy, RiskLevel
from rusa_agents.tools import ToolRegistry, ToolSpec


def test_tool_registry_enforces_policy():
    tools = ToolRegistry()
    tools.register(ToolSpec("safe.echo", "artifact.create"), lambda value: value)
    result = tools.execute("safe.echo", value="hello")
    assert result.success
    assert result.output == "hello"


def test_tool_registry_blocks_protected_action():
    tools = ToolRegistry(AutonomyPolicy())
    tools.register(
        ToolSpec("prod.deploy", "deployment.production", RiskLevel.CRITICAL),
        lambda: "deployed",
    )
    with pytest.raises(PermissionError):
        tools.execute("prod.deploy")
    assert tools.execute("prod.deploy", approval_granted=True).output == "deployed"


def test_tool_registry_does_not_run_unknown_tool():
    tools = ToolRegistry()
    with pytest.raises(LookupError):
        tools.execute("not.registered")
