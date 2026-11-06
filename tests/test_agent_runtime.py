import pytest
from pydantic import ValidationError
from rusa_agents.contracts import AgentPlan, TaskCreate, TaskStatus, validate_transition
from rusa_agents.policy import AutonomyPolicy
from rusa_agents.tools import ToolError, ToolRegistry

def make_task(**overrides):
    data = {"objective": "Improve agent orchestration reliability", "repository": "StrategyG/RusaAi", "work_branch": "feature/agent-runtime-test", "acceptance_criteria": ["Tests pass"]}
    data.update(overrides)
    return TaskCreate.model_validate(data)

def test_task_requires_safe_feature_branch():
    with pytest.raises(ValidationError): make_task(work_branch="main")
    with pytest.raises(ValidationError): make_task(work_branch="feature/../main")

def test_task_requires_branch_separation():
    with pytest.raises(ValidationError): make_task(base_branch="main", work_branch="main")

def test_state_machine_rejects_invalid_transition():
    with pytest.raises(ValueError): validate_transition(TaskStatus.QUEUED, TaskStatus.SUCCEEDED)

def test_state_machine_allows_retry_from_failed():
    validate_transition(TaskStatus.FAILED, TaskStatus.QUEUED)

def test_policy_is_task_scoped_and_fails_closed():
    policy = AutonomyPolicy()
    assert policy.evaluate("github.read_file", {"github.read_file"}, set()).allowed
    assert not policy.evaluate("shell.execute", {"shell.execute"}, set()).allowed
    assert policy.evaluate("github.merge_pull_request", set(), set()).requires_approval

def test_plan_schema_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AgentPlan.model_validate({"steps": [{"tool": "x", "arguments": {}, "purpose": "x", "extra": 1}]})

@pytest.mark.asyncio
async def test_tool_registry_rejects_unknown_tools():
    registry = ToolRegistry()
    with pytest.raises(ToolError): await registry.execute("shell.execute", {})
