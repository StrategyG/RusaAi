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

The `agent.role` field identifies one of the ten core-agent roles:

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

A core-agent role and a lifecycle stage must not be conflated. The existing six
AICode worker roles remain unchanged and are not renamed by this contract layer.

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
