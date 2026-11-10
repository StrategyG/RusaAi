from datetime import datetime, timedelta, timezone

import pytest

from rusa_agents.workflow_graph import (
    WorkflowGraph,
    WorkflowGraphError,
    WorkflowRunSpec,
    WorkflowTaskSpec,
    dependency_outcome,
)

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


def run_spec(**overrides):
    values = {
        "tenant_id": "tenant-a",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "domain": "software_engineering",
        "objective": "Build and verify a feature",
        "deadline_at": NOW + timedelta(hours=1),
        "created_by": "user:owner",
        "budget": {"max_parallel_tasks": 2},
    }
    values.update(overrides)
    return WorkflowRunSpec(**values)


def task(task_id, dependencies=(), **overrides):
    values = {
        "task_id": task_id,
        "domain": "software_engineering",
        "agent_id": "rusa.test_agent",
        "agent_version": "1.0.0",
        "capability": "testing.execute",
        "deadline_at": NOW + timedelta(minutes=30),
        "dependencies": tuple(dependencies),
    }
    values.update(overrides)
    return WorkflowTaskSpec(**values)


def test_valid_dag_returns_dependency_order():
    graph = WorkflowGraph(
        run_spec(),
        (
            task("test", ("build",)),
            task("release", ("test",)),
            task("build"),
        ),
    )
    assert graph.validate().topological_order() == ("build", "test", "release")


def test_missing_dependency_is_rejected():
    graph = WorkflowGraph(run_spec(), (task("test", ("missing",)),))
    with pytest.raises(WorkflowGraphError, match="missing dependency"):
        graph.validate()


def test_cycle_is_rejected():
    graph = WorkflowGraph(
        run_spec(),
        (task("a", ("b",)), task("b", ("a",))),
    )
    with pytest.raises(WorkflowGraphError, match="cycle"):
        graph.validate()


def test_fan_out_is_bounded():
    graph = WorkflowGraph(
        run_spec(),
        (
            task("root"),
            task("a", ("root",)),
            task("b", ("root",)),
            task("c", ("root",)),
        ),
        max_fan_out=2,
    )
    with pytest.raises(WorkflowGraphError, match="maximum fan-out"):
        graph.validate()


def test_task_deadline_cannot_exceed_workflow_deadline():
    graph = WorkflowGraph(
        run_spec(),
        (task("task", deadline_at=NOW + timedelta(hours=2)),),
    )
    with pytest.raises(WorkflowGraphError, match="deadline exceeds"):
        graph.validate()


def test_duplicate_task_ids_and_duplicate_edges_are_rejected():
    with pytest.raises(WorkflowGraphError, match="unique"):
        task("task", ("a", "a"))
    graph = WorkflowGraph(run_spec(), (task("a"), task("a")))
    with pytest.raises(WorkflowGraphError, match="duplicate task_id"):
        graph.validate()


@pytest.mark.parametrize(
    ("dependencies", "states", "expected"),
    [
        ((), {}, "ready"),
        (("a",), {"a": "succeeded"}, "ready"),
        (("a", "b"), {"a": "succeeded", "b": "running"}, "waiting"),
        (("a",), {"a": "failed"}, "blocked"),
        (("a",), {"a": "cancelled"}, "blocked"),
    ],
)
def test_dependency_outcome(dependencies, states, expected):
    assert dependency_outcome(dependencies, states) == expected
