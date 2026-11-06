import pytest

from rusa_agents.contracts import (
    CodingTask,
    RepositoryTarget,
    TaskRecord,
    TaskState,
)


def make_task() -> CodingTask:
    return CodingTask(
        "Add coding agent",
        RepositoryTarget("StrategyG/RusaAi", "main", "feature/test"),
        ["Tests pass"],
    )


def test_task_requires_acceptance_criteria():
    with pytest.raises(ValueError):
        CodingTask("x", RepositoryTarget("owner/repo"), [])


def test_invalid_transition_rejected():
    record = TaskRecord(make_task())
    with pytest.raises(ValueError):
        record.transition(TaskState.SUCCEEDED)


def test_valid_state_path():
    record = TaskRecord(make_task())
    for state in (
        TaskState.OBSERVING,
        TaskState.PLANNING,
        TaskState.EXECUTING,
        TaskState.VERIFYING,
        TaskState.AWAITING_APPROVAL,
    ):
        record.transition(state)
    assert record.state == TaskState.AWAITING_APPROVAL
