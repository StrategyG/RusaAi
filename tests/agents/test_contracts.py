import pytest

from rusa_agents.contracts import AgentRole, TaskRecord, TaskSpec, TaskState


def make_task():
    return TaskSpec(
        objective="Inspect source",
        acceptance_criteria=("Report findings",),
        tenant_id="tenant-a",
        project_id="project-a",
        capability="review",
    )


def test_task_requires_acceptance_criteria():
    with pytest.raises(ValueError):
        TaskSpec(
            objective="Inspect",
            acceptance_criteria=(),
            tenant_id="tenant-a",
            project_id="project-a",
            capability="review",
        )


def test_state_machine_rejects_skipping_execution():
    record = TaskRecord(make_task())
    with pytest.raises(ValueError):
        record.transition(TaskState.SUCCEEDED)


def test_state_machine_accepts_valid_path():
    record = TaskRecord(make_task())
    for state in (TaskState.QUEUED, TaskState.RUNNING, TaskState.VERIFYING, TaskState.SUCCEEDED):
        record.transition(state)
    assert record.state == TaskState.SUCCEEDED


def test_agent_role_contract():
    assert AgentRole.SUPERVISOR.value == "supervisor"
