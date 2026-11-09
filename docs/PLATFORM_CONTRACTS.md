# RUSA-AI Platform Contracts v1.0

This package introduces versioned, transport-neutral contracts for cross-domain
agent tasks, results, and run events. It is the first contract layer for the
four RUSA-AI domains and the eight-stage DevOps lifecycle.

## Scope and non-claims

The contracts define JSON payload shape and local validation invariants. They do
**not** implement or claim a message broker, transactional outbox, durable event
store, event replay service, gRPC transport, JWS signing, mTLS, or agent registry.
The existing PostgreSQL AICode queue remains the current durable coding-task
runtime. Existing A2A task-envelope v1.0 remains supported and is not silently
replaced by these platform contracts.

## Domain and role axes

The `domain` field is one of:

- `rusa_intelligence`
- `autonomous_software_engineering`
- `rusa_business`
- `rusa_ecosystem`

The `agent.agent_type` field keeps two source-defined identity sets separate:

- `core_agent`: identifies one of the ten core-agent roles through
  `agent.core_role`.
- `devops_stage_agent`: identifies one of the eight lifecycle specialists
  through `agent.stage_agent_id` (`rusa_plan_agent` through
  `rusa_monitor_agent`). Its task `stage` must match that specialist.

The ten core-agent roles are:

- `supreme_orchestrator`
- `system_architect`
- `planner`
- `task_decomposer`
- `software_engineer`
- `devops`
- `qa_testing`
- `security_policy`
- `memory_knowledge`
- `evaluation_reporting`

The optional `stage` field is a separate lifecycle axis:

`plan`, `code`, `build`, `test`, `release`, `deploy`, `operate`, `monitor`.

The eight lifecycle specialists and the ten core agents are separate source
lists. The supplied documents do not define a one-to-one identity mapping
between the DevOps Orchestrator/Security Guard and the ten core agents, so the
contract does not invent one. The existing six AICode worker roles remain
unchanged and are not renamed by this contract layer.

## Compatibility with existing task contracts

The platform v1.0 contracts are not wire-compatible replacements for the
existing A2A task-envelope v1.0 or the six-role AICode runtime contracts.
Keep explicit adapters at integration boundaries; do not rename fields or
reinterpret statuses implicitly.

The source specifications use different status vocabularies:

- The ten-agent communication contract uses `PENDING`, `READY`, `RUNNING`,
  `BLOCKED`, `NEEDS_APPROVAL`, `REVIEW`, `COMPLETED`, `FAILED`, and
  `CANCELLED`.
- The DevOps lifecycle uses `queued`, `running`, `passed`, `failed`,
  `blocked`, `needs_approval`, `cancelled`, and `skipped`.
- The A2A envelope has its own optional status values, including
  `proposed`, `accepted`, `in_progress`, `submitted`, `reviewing`,
  `approved`, and `changes_requested`.

A future adapter may use this semantic crosswalk, while preserving the original
status and lifecycle stage in audit metadata:

| Source meaning | Platform representation | Constraint |
|---|---|---|
| `PENDING`, `READY`, `queued`, `proposed`, `accepted` | Task/run remains queued or accepted; no result yet | Do not fabricate an agent result before dispatch |
| `RUNNING`, `in_progress` | Run/agent execution state or event | Not a terminal result |
| `submitted` | Preserve as an A2A envelope state until its dispatch semantics are confirmed | Do not assume it means execution has started |
| `BLOCKED`, `blocked` | Result status `blocked` or `run.blocked` | Preserve the blocking reason |
| `NEEDS_APPROVAL`, `needs_approval` | `awaiting_approval` | Only when a real human/policy gate is pending |
| `reviewing` | Preserve as review state/event; use `awaiting_approval` only if an actual approval gate is pending | Do not collapse all review work into approval |
| `COMPLETED`, `passed` | Result status `succeeded` | Requires at least one passing evidence record |
| `approved` | Event `human.approval.granted` | Approval is a control-plane decision, not proof that execution succeeded |
| `FAILED`, `changes_requested` | Result status `failed` or an explicit review event | Include a structured error/findings object; changes requested may instead return work to Code |
| `CANCELLED`, `cancelled` | Result status `cancelled` or `run.cancelled` | Preserve whether cancellation was acknowledged and whether side effects were reconciled |
| `REVIEW`, `skipped` | Explicit review/policy event | Do not collapse these into success or cancellation |

The A2A message types (`task.create`, `task.accepted`, `task.status`,
`task.result`, `task.blocked`, `task.review`, and `task.cancel`) are
message operations, not synonyms for platform run-event types. The platform
run-event schema now includes the lifecycle vocabulary
`plan.completed`, `code.completed`, `build.completed`,
`test.completed`, `release.approved`, `deploy.completed`,
`operate.incident_detected`, `operate.recovered`,
`monitor.alert_triggered`, `monitor.feedback_created`,
`workflow.blocked`, and `workflow.completed`.

## Contract files

| Contract | Purpose |
|---|---|
| `schemas/platform/agent-task-v1.schema.json` | Tenant-bound, idempotent, deadline-bound dispatch envelope with an explicit allow decision |
| `schemas/platform/agent-result-v1.schema.json` | Result, artifact digests, and evidence tied to an execution interval |
| `schemas/platform/run-event-v1.schema.json` | Run-scoped event envelope with sequence, state version, correlation, and causation identifiers |

## Validation API

Use `rusa_agents.platform_contracts`:

- `validate_agent_task(payload)`
- `validate_agent_result(payload)`
- `validate_run_event(payload)`
- `validate_run_event_batch(events)`

Validation is fail-closed. A dispatchable task must carry an explicit policy
authorization ID and `decision=allow`; a denied or approval-pending action must
not be dispatched. A succeeded agent result must include at least one verification-evidence
record, and every included record must pass; missing, failed, or held evidence
cannot be reported as success. Event batches cannot mix tenants, runs, or correlation IDs, and their
sequence/state versions cannot move backwards.

## Security and implementation boundary

- Tenant identity is a required contract field but must still be bound to
  authenticated runtime context; never trust a model-generated tenant ID.
- `authorization_id` is a reference to a trusted policy decision, not proof
  that a consumer has verified that decision.
- Artifact and evidence SHA-256 values bind metadata to bytes; the referenced
  bytes and their provenance must be verified by a trusted consumer.
- The event schema does not imply event authenticity. Detached JWS signatures
  and key rotation remain future ADR work.
- Sequence allocation, durable append, duplicate suppression, replay cursors,
  and transactional publication remain event-store/outbox responsibilities.
- No production merge, release, deploy, permission, secret, or destructive
  action is authorized by the existence of these contracts.
