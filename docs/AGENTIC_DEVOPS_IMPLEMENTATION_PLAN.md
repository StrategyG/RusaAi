# RUSA-AI Agentic DevOps — implementation plan

## Source and scope

This implementation plan follows the approved project reference
`RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE.md` and the proposed
`ADR-COMM-001` communication architecture. It is intentionally incremental:
existing AICode queue, workers, API, and sandbox are not rewritten just to
match a proposed directory layout.

## Current repository baseline

- Existing AICode orchestration is an in-process Python orchestrator.
- Existing persistent execution is the AICode PostgreSQL task queue with
  lease fencing, retries, cancellation, and tenant-scoped idempotency.
- Existing worker roles (analyst, architect, developer, debugger, tester,
  security) are not the same as the eight DevOps lifecycle agents.
- Existing A2A task envelope schema is a separate protocol contract and is
  not silently replaced by these DevOps-specific contracts.
- RabbitMQ, Kafka, durable workflow outbox/event log, gRPC/Protobuf RPC,
  message signing, and an independent DevOps Orchestrator are not yet
  implemented by this foundation.

## Agent registry

| Stage | Agent ID | Main responsibility |
|---|---|---|
| Plan | `rusa_plan_agent` | Requirements, backlog, acceptance criteria, risk assessment |
| Code | `rusa_code_agent` | Branch-scoped implementation, tests, PR and change summary |
| Build | `rusa_build_agent` | Reproducible build, digest, manifest and SBOM |
| Test | `rusa_test_agent` | Unit/integration/regression/security evidence |
| Release | `rusa_release_agent` | Release candidate, manifest, changelog and approval request |
| Deploy | `rusa_deploy_agent` | Approved environment deployment, health and rollback evidence |
| Operate | `rusa_operate_agent` | Allowlisted runbook actions and incident recovery |
| Monitor | `rusa_monitor_agent` | Read-only metrics/logs/traces, SLO and feedback |
| Cross-cutting | `rusa_security_guard_agent` | Policy, privacy, security and audit checks |

The DevOps Orchestrator is the workflow coordinator, not a ninth specialist.
The Security Guard is cross-cutting and is not part of the eight-stage
delivery chain. The ten system-wide agent roles, orchestrator authority
boundary, and ADR-COMM-001 envelope mapping are documented in
[DEVOPS_AGENT_INTEGRATION.md](DEVOPS_AGENT_INTEGRATION.md).

## Phase 1 — contract foundation (this change)

- Add versioned JSON Schema contracts for agent task, agent result, and
  durable workflow event.
- Validate task-to-agent/stage mapping.
- Require explicit allowed tools, write scope, network policy, approval
  boundary, budget and audit context.
- Require evidence on successful results and an approval reference when
  a result is waiting for approval.
- Validate workflow event IDs, tenant/run/task correlation, W3C traceparent,
  event-specific payloads, lease epochs and state versions.
- Add deterministic unit tests for valid and invalid contracts.

**Exit criteria:** contracts validate in CI. This phase does not execute
agents, publish messages, persist workflow state, or authorize production
side effects.

## Phase 2 — safe dry-run workflow

- Implement the DevOps Orchestrator with dummy agents only.
- Use the canonical task/result/event contracts from Phase 1.
- Run Plan → Code → Build → Test → Release → Deploy → Operate → Monitor
  against synthetic repository and service fixtures.
- Enforce human approval boundaries for merge, release, deployment,
  secret/permission changes and destructive actions.
- Prove failed gates stop downstream execution and Monitor feedback can
  create a new Plan request.

**Exit criteria:** one full synthetic workflow completes with auditable
evidence and no external side effects.

## Phase 3 — durable workflow and inter-agent communication

- Select and implement the workflow store and transactional outbox.
- Add durable command queue and event log as separately governed channels.
- Implement idempotent consumers, at-least-once delivery, dead-letter
  quarantine, lease epoch/state-version fencing and cancellation epochs.
- Add RPC deadlines, trace propagation, service identity and message
  signing/verification.
- Keep replay and analytics paths unable to execute tools.

**Exit criteria:** crash/restart, redelivery, replay, cancel, and partial
failure tests preserve canonical workflow state without duplicate effects.

## Phase 4 — specialist execution and QA

- Implement the eight agents behind per-agent identities and capability
  allowlists.
- Keep Code execution in isolated sandbox workspaces.
- Bind every build/test/release artifact to exact source commit and digest.
- Require independent QA evidence before release publication.
- Keep deployment production approvals outside agent authority.

**Exit criteria:** a staging workflow produces a signed release candidate
whose tests and provenance match the exact source revision.

## Phase 5 — production operations and feedback

- Implement Operate and Monitor against approved telemetry and runbooks.
- Add SLO dashboards, alert routing, incident records, budget controls,
  kill-switch and policy enforcement.
- Test rollback, backup/restore, tenant isolation and disaster recovery.
- Feed monitor findings back to Plan through an audited workflow event.

**Exit criteria:** controlled closed-loop operation passes security, SRE,
and product-owner sign-off.

## Non-negotiable controls

- Code agents cannot write protected branches or merge their own PRs.
- Test agents cannot report PASS without test evidence for the exact commit.
- Release agents cannot bypass failed CI, vulnerability, provenance or QA gates.
- Deploy agents cannot read raw secrets or deploy without explicit approval.
- Operate agents are limited to approved runbooks; destructive actions require
  human approval.
- Monitor agents are read-only.
- Every handoff carries immutable artifact/evidence references, not only text.
- All tenant, task, policy, budget, approval and cancellation context is
  propagated to child tasks.
