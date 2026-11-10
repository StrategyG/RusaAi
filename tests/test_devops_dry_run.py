import pytest

from rusa_agents.devops.orchestrator import (
    AGENT_BY_STAGE,
    SECURITY_GUARD_ID,
    DevOpsDryRunOrchestrator,
    SyntheticDevOpsAgent,
)


def _approval_refs():
    return {
        "release": "approval://dry-run/release-approved",
        "deploy": "approval://dry-run/deploy-approved",
    }


def _run(orchestrator):
    return orchestrator.run(
        user_request="Validate a synthetic service change from planning through monitoring.",
        repository="example/rusa-demo",
        acceptance_criteria=[
            "All required checks pass.",
            "The release is bound to the tested source commit and artifact digest.",
        ],
        service="rusa-demo",
        metrics_window="last-15m",
    )


def test_dry_run_completes_all_eight_stages_with_security_guard_reviews():
    report = _run(DevOpsDryRunOrchestrator(approval_refs=_approval_refs()))

    assert report.status == "completed"
    assert report.external_side_effects is False
    specialist_tasks = [
        task for task in report.tasks if task["agent_id"] != SECURITY_GUARD_ID
    ]
    security_tasks = [
        task for task in report.tasks if task["agent_id"] == SECURITY_GUARD_ID
    ]
    assert [task["stage"] for task in specialist_tasks] == [
        "plan", "code", "build", "test", "release", "deploy", "operate", "monitor"
    ]
    assert len(security_tasks) == 8
    assert all(task["constraints"]["allowed_tools"] == [] for task in report.tasks)
    assert all(task["constraints"]["network_access"] is False for task in report.tasks)
    assert all(task["constraints"]["write_scope"] == [] for task in report.tasks)
    assert sum(event["type"] == "task.completed" for event in report.events) == 8
    assert sum(event["type"] == "audit.recorded" for event in report.events) == 8
    assert sum(event["type"] == "workflow.completed" for event in report.events) == 1


def test_failed_test_gate_stops_release_and_all_downstream_stages():
    orchestrator = DevOpsDryRunOrchestrator(
        approval_refs=_approval_refs(),
        agents={
            AGENT_BY_STAGE["test"]: SyntheticDevOpsAgent(
                status_by_stage={"test": "failed"}
            )
        },
    )
    report = _run(orchestrator)

    assert report.status == "failed"
    specialist_stages = [
        task["stage"]
        for task in report.tasks
        if task["agent_id"] != SECURITY_GUARD_ID
    ]
    assert specialist_stages == ["plan", "code", "build", "test"]
    assert "release" not in specialist_stages
    assert any(event["type"] == "task.failed" for event in report.events)
    assert any(event["type"] == "workflow.blocked" for event in report.events)
    assert not any(event["type"] == "workflow.completed" for event in report.events)


def test_release_waits_for_approval_and_does_not_run_deploy():
    report = _run(DevOpsDryRunOrchestrator())

    assert report.status == "needs_approval"
    specialist_stages = [
        task["stage"]
        for task in report.tasks
        if task["agent_id"] != SECURITY_GUARD_ID
    ]
    assert specialist_stages == ["plan", "code", "build", "test", "release"]
    assert "deploy" not in specialist_stages
    assert any(
        event["type"] == "audit.recorded"
        and event["payload"]["action"] == "approval.requested"
        for event in report.events
    )


def test_security_guard_block_stops_downstream_execution():
    orchestrator = DevOpsDryRunOrchestrator(
        agents={
            SECURITY_GUARD_ID: SyntheticDevOpsAgent(
                status_by_stage={"plan": "blocked"}
            )
        }
    )
    report = _run(orchestrator)

    assert report.status == "blocked"
    assert [task["stage"] for task in report.tasks] == ["plan", "plan"]
    assert any(event["type"] == "policy.violation" for event in report.events)
    assert any(event["type"] == "workflow.blocked" for event in report.events)


def test_dry_run_blocks_production_deploy_in_staging_only_phase4e():
    orchestrator = DevOpsDryRunOrchestrator(
        environment="production",
        approval_refs={"release": "approval://dry-run/release-approved"},
    )
    report = _run(orchestrator)

    assert report.status == "blocked"
    assert not any(
        task["stage"] == "deploy" and task["agent_id"] == AGENT_BY_STAGE["deploy"]
        for task in report.tasks
    )
    blocked = [event for event in report.events if event["type"] == "workflow.blocked"]
    assert blocked[-1]["payload"]["stage"] == "deploy"
    assert blocked[-1]["payload"]["reason_code"] == "staging_only"


def test_monitor_feedback_creates_a_causally_linked_new_plan_request():
    orchestrator = DevOpsDryRunOrchestrator(
        approval_refs=_approval_refs(),
        agents={
            AGENT_BY_STAGE["monitor"]: SyntheticDevOpsAgent(
                monitor_feedback=True
            )
        },
    )
    report = _run(orchestrator)

    assert report.status == "completed"
    assert report.feedback_request is not None
    assert report.feedback_request["type"] == "task.request"
    assert report.feedback_request["payload"]["capability"] == "plan"
    assert report.feedback_request["causation_id"] == next(
        event["message_id"]
        for event in report.events
        if event["type"] == "task.completed"
        and event["payload"]["stage"] == "monitor"
    )


def test_result_with_wrong_source_commit_blocks_the_workflow():
    orchestrator = DevOpsDryRunOrchestrator(
        approval_refs=_approval_refs(),
        agents={
            AGENT_BY_STAGE["build"]: SyntheticDevOpsAgent(
                source_commit_by_stage={"build": "c" * 40}
            )
        },
    )
    report = _run(orchestrator)

    assert report.status == "blocked"
    assert "source_commit" in report.blockers[0]
    assert not any(
        task["stage"] == "test" and task["agent_id"] == AGENT_BY_STAGE["test"]
        for task in report.tasks
    )

def test_dry_run_rejects_non_synthetic_agent_implementations():
    class ExternalAgent:
        def run(self, task):
            raise AssertionError("external agents must never be invoked in dry-run")

    with pytest.raises(TypeError, match="SyntheticDevOpsAgent fixtures only"):
        DevOpsDryRunOrchestrator(
            agents={AGENT_BY_STAGE["plan"]: ExternalAgent()}
        )

def test_production_deploy_runs_only_with_explicit_approval_reference():
    orchestrator = DevOpsDryRunOrchestrator(
        environment="production",
        approval_refs=_approval_refs(),
    )
    report = _run(orchestrator)

    assert report.status == "completed"
    deploy_tasks = [
        task for task in report.tasks
        if task["stage"] == "deploy"
    ]
    assert len(deploy_tasks) == 2
    assert all(task["input"]["environment"] == "production" for task in deploy_tasks)
    assert all(task["constraints"]["approval_ref"] == _approval_refs()["deploy"] for task in deploy_tasks)

def test_cancelled_stage_emits_task_cancelled_not_task_failed():
    orchestrator = DevOpsDryRunOrchestrator(
        agents={
            AGENT_BY_STAGE["test"]: SyntheticDevOpsAgent(
                status_by_stage={"test": "cancelled"}
            )
        }
    )
    report = _run(orchestrator)

    assert report.status == "cancelled"
    assert any(event["type"] == "task.cancelled" for event in report.events)
    assert not any(event["type"] == "task.failed" for event in report.events)
    assert not any(
        task["stage"] == "release" and task["agent_id"] == AGENT_BY_STAGE["release"]
        for task in report.tasks
    )


def test_skipped_stage_is_audited_and_stops_downstream_stages():
    orchestrator = DevOpsDryRunOrchestrator(
        agents={
            AGENT_BY_STAGE["test"]: SyntheticDevOpsAgent(
                status_by_stage={"test": "skipped"}
            )
        }
    )
    report = _run(orchestrator)

    assert report.status == "blocked"
    assert any(
        event["type"] == "audit.recorded"
        and event["payload"]["action"] == "test.skipped"
        for event in report.events
    )
    assert not any(event["type"] == "task.failed" for event in report.events)

def test_dry_run_agent_registry_is_immutable():
    orchestrator = DevOpsDryRunOrchestrator()
    with pytest.raises(TypeError):
        orchestrator.agents[AGENT_BY_STAGE["plan"]] = SyntheticDevOpsAgent()

