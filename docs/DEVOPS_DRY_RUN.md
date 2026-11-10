# RUSA AI DevOps — safe lifecycle dry-run

## Purpose

This is the Phase 2 simulation layer described in the Agentic DevOps implementation plan. It exercises the eight lifecycle handoffs before any specialist agent is connected to repository, build, deployment, telemetry, or operational tools.

## Execution model

The DevOpsDryRunOrchestrator in rusa_agents.devops.orchestrator runs these stages in process:

1. Plan
2. Code
3. Build
4. Test
5. Release
6. Deploy
7. Operate
8. Monitor

A synthetic Security Guard review runs after each successful specialist stage. The Security Guard is cross-cutting and is not counted as a ninth lifecycle stage.

Every generated task and result is validated against the Phase 1 JSON Schemas. Every emitted workflow event is validated before it is returned in the dry-run report. Result validation binds the response to the task ID, workflow ID, tenant, agent, stage, attempt, source commit, and artifact digest where those values are part of the task.

## Safety properties

- Synthetic agents only; no LLM provider is called.
- No repository API, Git operation, CI service, deployment client, telemetry service, or runbook is invoked.
- Task capabilities are empty, network access is disabled, write scopes are empty, and the cost/tool-call budget is zero.
- Release and Deploy stop with needs_approval when an approval reference is absent.
- A production Deploy task is not created until an approval reference is present.
- Failed, blocked, invalid, or approval-pending results stop downstream stages.
- Security Guard reviews are read-only and produce audit or policy events.
- Monitor feedback produces a causally linked task.request event for a new Plan workflow. The event is returned in the report; it is not published to a queue.

## Approval limitation

Approval references supplied to this dry-run are synthetic fixture values. Their presence tests contract and handoff behavior only. They are not proof of a human decision and must never be accepted by a production Policy Engine. Production execution must resolve the reference against an authenticated approval record bound to the action, resource, tenant, and commit or artifact digest.

## State and transport boundary

The dry-run is intentionally ephemeral. It does not introduce a second canonical workflow store or queue and does not change the existing AICode PostgreSQL queue. It does not implement RabbitMQ, Kafka, a transactional outbox, durable event replay, gRPC, message signing, lease fencing, or production cancellation. Those remain later phases.

## Example

    from rusa_agents.devops.orchestrator import DevOpsDryRunOrchestrator

    orchestrator = DevOpsDryRunOrchestrator(
        approval_refs={
            "release": "approval://dry-run/release-approved",
            "deploy": "approval://dry-run/deploy-approved",
        }
    )
    report = orchestrator.run(
        user_request="Validate a synthetic service change.",
        repository="example/rusa-demo",
        acceptance_criteria=["Required checks pass."],
    )
    assert report.status == "completed"
    assert report.external_side_effects is False

To simulate a failed Test gate or Security Guard block, inject a SyntheticDevOpsAgent with a stage-specific status_by_stage mapping. To simulate Monitor feedback, inject the Monitor synthetic agent with monitor_feedback=True.

## Automated coverage

tests/test_devops_dry_run.py covers the complete eight-stage path, cross-cutting Security Guard review, zero-capability execution, release and production deployment approval gates, failed-stage stop behavior, commit-evidence mismatch rejection, and Monitor-to-Plan feedback causation.
