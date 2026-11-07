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


def test_production_deploy_requires_approval_before_deploy_task_is_created():
    orchestrator = DevOpsDryRunOrchestrator(
        environment="production",
        approval_refs={"release": "approval://dry-run/release-approved"},
    )
    report = _run(orchestrator)

    assert report.status == "needs_approval"
    assert not any(
        task["stage"] == "deploy" and task["agent_id"] == AGENT_BY_STAGE["deploy"]
        for task in report.tasks
    )
    blocked = [event for event in report.events if event["type"] == "workflow.blocked"]
    assert blocked[-1]["payload"]["stage"] == "deploy"
    assert blocked[-1]["payload"]["reason_code"] == "approval_required"


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
    class WrongCommitBuildAgent(SyntheticDevOpsAgent):
        def run(self, task):
            result = super().run(task)
            if task["stage"] == "build":
                result["audit"]["source_commit"] = "c" * 40
            return result

    orchestrator = DevOpsDryRunOrchestrator(
        approval_refs=_approval_refs(),
        agents={AGENT_BY_STAGE["build"]: WrongCommitBuildAgent()},
    )
    report = _run(orchestrator)

    assert report.status == "blocked"
    assert "source_commit" in report.blockers[0]
    assert not any(
        task["stage"] == "test" and task["agent_id"] == AGENT_BY_STAGE["test"]
        for task in report.tasks
    )
