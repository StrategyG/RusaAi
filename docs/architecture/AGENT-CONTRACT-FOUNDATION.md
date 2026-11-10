# RUSA AI Agent Contract Foundation

**Phase:** Agentic DevOps Phase 1 — Agent Contract Foundation  
**Contract version:** 1  
**Implementation status:** Typed contracts, JSON Schemas, and a no-side-effect dummy workflow are implemented. Production orchestration and broker delivery remain future work.

## Scope

This module establishes a versioned contract boundary for the eight lifecycle specialists described in the Agentic DevOps architecture:

| Stage | Canonical agent ID |
|---|---|
| Plan | `rusa_plan_agent` |
| Code | `rusa_code_agent` |
| Build | `rusa_build_agent` |
| Test | `rusa_test_agent` |
| Release | `rusa_release_agent` |
| Deploy | `rusa_deploy_agent` |
| Operate | `rusa_operate_agent` |
| Monitor | `rusa_monitor_agent` |

The contract also supports non-stage identities such as `rusa_devops_orchestrator` and `rusa_security_guard_agent`. This change defines identity models; it does not claim that all ten agents are implemented or registered as executable workers.

The separate Complete Agent Documentation lists a ten-role cross-functional catalog: Supreme Orchestrator, System Architect, Planner, Task Decomposer, Software Engineer, DevOps, QA & Testing, Security & Policy, Memory & Knowledge, and Evaluation & Reporting. This phase does not collapse that catalog into the eight lifecycle stages or invent one-to-one mappings. The registry, ownership boundaries, and per-agent tool scopes must be explicitly reviewed before those identities become executable workers.

## Contract artifacts

- `rusa_agents/devops/contracts.py` — Pydantic v2 models and validation rules.
- `schemas/devops/agent-identity.schema.json` — stable agent identity and declared capabilities.
- `schemas/devops/agent-task.schema.json` — delegation request, constraints, inputs and acceptance criteria.
- `schemas/devops/agent-result.schema.json` — status, outputs, evidence, risks and unresolved issues.
- `schemas/devops/workflow-event.schema.json` — versioned event envelope and event-specific payloads.
- `rusa_agents/devops/workflow.py` — in-process dummy workflow runner for the Phase 1 exit criterion.
- `tests/agents/test_devops_contracts.py`, `tests/agents/test_devops_dummy_workflow.py`, and `tests/contracts/test_devops_schemas.py` — contract, end-to-end handoff, and JSON Schema conformance tests.

All contracts reject undeclared fields. Task requests and results carry an explicit attempt number so retries cannot silently reuse evidence from another attempt. Timestamps must include a timezone. Trace context follows the W3C version-00 `traceparent` format. Artifact references carry a SHA-256 digest so a result can be bound to the exact evidence version that was reviewed; the consumer must still authorize access to the reference under the authenticated tenant context.

## Vocabulary and compatibility

The lifecycle contract uses the lower-case task statuses defined by the Agentic DevOps document:

- `queued`, `running`, `passed`, `failed`, `blocked`
- `needs_approval`, `cancelled`, `skipped`

The current AICode runtime has a separate state machine. `map_aicode_task_state()` provides an explicit adapter from its existing states to the lifecycle vocabulary. For example, `succeeded` maps to `passed`, and `awaiting_approval` maps to `needs_approval`. The existing AICode state machine and queue values are not renamed by this contract change.

The field `agent_id` identifies the assigned recipient and corresponds to `target_agent` in the earlier Complete Agent Communication Contract. The source identity is carried in `source_agent`.

## Event envelope

The event schema preserves the ADR envelope fields: schema version, event type, message ID, tenant ID, run ID, task ID and attempt when task-scoped, correlation and causation IDs, W3C trace context, producer, timestamp, and typed payload.

Supported event types are:

- `task.request`, `task.accepted`, `task.progress`
- `tool.invocation`
- `task.completed`, `task.failed`
- `policy.violation`
- `task.cancel.requested`, `task.cancelled`
- `audit.recorded`

Task lifecycle events require both `task_id` and `attempt`. Policy and audit events may be scoped to a workflow rather than one task. Payloads are validated against the schema associated with their event type.

## Execution and security boundary

This phase deliberately reuses the existing PostgreSQL-backed AICode task queue rather than introducing a second durable queue. The state mapper is the compatibility seam; a general workflow scheduler, inbox/outbox, event relay, and workflow-level state store are not introduced here.

The communication ADR describes gRPC/Protobuf for internal synchronous calls, RabbitMQ for durable commands, and Kafka or an equivalent event log for event distribution. Those are target architecture choices, not capabilities delivered by this contract-only change. Broker adoption must be justified against measured load and the existing queue before implementation.

Declared `allowed_tools`, `write_scope`, and `approval_required` values are contract data, not an authorization decision. The Policy Engine and Tool Gateway remain the enforcement points for every actual operation. Model output cannot grant permissions. Production merge, infrastructure changes, and deployment remain subject to the human approval requirements in the architecture.

## Phase 1 dummy workflow

`DummyWorkflowRunner` executes one stage-scoped specialist task entirely in-process. It validates the task recipient and stage against a registered specialist identity, rejects any declared tool, network, write, or approval capability, and refuses expired tasks. It emits a causally linked `task.request` → `task.accepted` → `task.completed` event sequence and returns a typed `passed` result with a SHA-256-bound evidence reference. The completion event references a separately serialized result payload and its digest; both result and evidence bytes are returned in memory for a future artifact-store adapter. The dummy runner does not invoke the Tool Gateway, access a repository, access the network, or publish to a broker. Its purpose is to prove contract-to-result handoff safely; it is not the production orchestrator.

## Current limitations and next phase

- The eight lifecycle specialist implementations and the production DevOps Orchestrator are not created by this phase; the dummy runner is a testable execution harness only.
- No broker publishing, event replay, outbox transaction, durable workflow event store, or gRPC service is added.
- The existing PostgreSQL-backed AICode queue remains the only durable queue and is not yet a general multi-domain workflow engine.
- The Policy Engine and Tool Gateway remain the enforcement points for actual operations; contract declarations are not runtime authorization.

Phase 1's exit criterion is covered by the dummy workflow and its end-to-end tests. The next implementation phase is the Plan, Code, Build and Test specialists, with isolated branch changes, build/test evidence bound to the exact commit, and stop-on-failed-gate behavior.
