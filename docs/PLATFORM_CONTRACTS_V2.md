# RUSA AI Platform Contracts v2

**Status:** Contract foundation and code-bearing P0 control-plane services implemented; live AICode runtime cutover remains open  
**Version:** Agent Manifest 1.0; Task, Result, Event, and Task Record 2.0  
**Date:** 10 November 2026  
**Implementation:** `rusa_agents/platform_contracts.py`

## 1. Contract layers and compatibility boundary

RUSA AI distinguishes three contract layers:

1. **Platform control-plane contracts** — canonical identity, task, result, event, and state snapshot models.
2. **Domain execution contracts** — current AICode dataclasses and future domain-specific task/result payloads.
3. **Transport protocols** — the existing A2A wire envelope and current PostgreSQL task queue remain separately versioned.

The internal `TaskEnvelopeV2` does not replace the existing A2A wire protocol. The existing `schemas/a2a/task-envelope.schema.json` with `protocol_version: 1.0` remains unchanged. A future adapter may map external A2A messages to the internal task envelope.

The current PostgreSQL `aicode_jobs` queue remains the durable execution mechanism. Its status strings remain exactly `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, and `cancelled`. No second queue, broker migration, or live runtime cutover is part of this contract increment.

## 2. Checked-in contracts

| Contract | Version | Schema |
|---|---:|---|
| Agent Manifest | 1.0 | `schemas/platform/agent-manifest.v1.schema.json` |
| Universal Task Envelope | 2.0 | `schemas/platform/task-envelope.v2.schema.json` |
| Agent Result | 2.0 | `schemas/platform/agent-result.v2.schema.json` |
| Workflow Event | 2.0 | `schemas/platform/workflow-event.v2.schema.json` |
| AICode Task Record snapshot | 2.0 | `schemas/platform/task-record.v2.schema.json` |

The Workflow Event schema includes typed payload definitions and conditional validation for:

- `task.accepted`, `task.progress`, `tool.invocation`.
- `task.completed`, `task.failed`.
- `policy.decision`, `policy.violation`.
- `task.cancel.requested`, `task.cancelled`.
- `approval.requested`, `approval.decided`.
- `validation.verdict`, `audit.recorded`.

The Task Envelope is the `task.request` contract. Completion and failure event payloads carry result references and state/lease versions; a full Agent Result is separately addressable.

The DevOps lifecycle event vocabulary remains accepted as generic workflow events while domain-specific stage payloads are versioned separately: `plan.completed`, `code.completed`, `build.completed`, `test.completed`, `release.approved`, `deploy.completed`, `operate.incident_detected`, `operate.recovered`, `monitor.alert_triggered`, `monitor.feedback_created`, `workflow.blocked`, and `workflow.completed`.

All schemas use JSON Schema Draft 2020-12 and are generated from Pydantic models. Regenerate and verify them with:

```bash
python scripts/export_platform_contract_schemas.py
python scripts/export_platform_contract_schemas.py --check
```

CI runs the schema drift check before lint and tests.

## 3. Agent Manifest 1.0

An agent manifest declares identity, domain, capabilities, input/output schema references, runtime binding, model policy, tool allowlist, tenant/resource scope, deadline/budget policy, policy profile, approval policy, evaluation profile, health snapshot, and owner.

Important rules:

- `agent_id` is a stable lowercase identifier; `version` is semantic versioning.
- A published manifest version is immutable. Lifecycle status and the runtime health snapshot may change without changing the executable contract; content changes require a new semantic version.
- `status` distinguishes draft, validated, active, suspended, deprecated, and revoked.
- The transition validator rejects invalid status changes and edits to published manifest content.
- `health` records a bounded runtime observation (`unknown`, `healthy`, `degraded`, `unhealthy`, or `maintenance`) and optional timezone-aware check time.
- Runtime and policy profiles are required; a manifest is not a permission grant by itself.
- Tool permissions still require deterministic policy enforcement at the Tool Gateway.
- `tenant_scope` supports tenant, system, or explicit tenant allowlist scope.

The Pydantic model and transition helper validate the contract. A separate PostgreSQL agent-registry service and migration now implement registration, validation, activation, suspension, deprecation, revocation, health updates, and audited lifecycle transitions. The registry service is not yet wired into live AICode worker startup or API dispatch.

## 4. Universal Task Envelope 2.0

Every internal platform task is bound to:

- `tenant_id`, `workflow_id`, `run_id`, and `task_id`.
- `message_id`, `correlation_id`, optional `causation_id`, and optional W3C `traceparent`.
- `domain`, `agent_id`, `agent_version`, and `capability`.
- Attempt, optional idempotency key, priority, deadline, and bounded execution budget.
- Input payload or immutable input references with SHA-256 digest.
- Policy context, explicit `approval_required`, and optional approval reference.

The task deadline must be timezone-aware and later than creation time. Trace context must use W3C version 00 format with non-zero trace and parent-span IDs. Budget values are hard-limit declarations for the scheduler/runtime to enforce; validating a task envelope does not enforce runtime limits.

Do not persist credentials in input, policy context, or event payloads. Credential-shaped keys such as `api_key`, `access_token`, `authorization`, `password`, `client_secret`, and `private_key` are rejected by Pydantic validation. Secrets must remain in a secret manager or scoped tool service, not in task messages.

The legacy adapter uses a one-hour execution deadline and a 100-tool-call budget when no explicit budget is supplied. These are compatibility defaults for this adapter, not the final tenant policy.

## 5. Agent Result 2.0 and validation verdict

An agent result is correlated to the tenant, workflow, run, task, agent version, capability, and attempt that produced it.

The result separates:

- `output` — structured output data.
- `output_refs` — immutable content references with SHA-256 digests.
- `evidence_refs` — immutable evidence references with SHA-256 digests.
- `legacy_evidence` — compatibility-only data from the current AICode WorkerResult.

**Legacy evidence is not a QA verdict.** Existing AICode evidence dictionaries are preserved in `legacy_evidence` during migration. A validation verdict is a separate typed event whose `subject_digest` identifies the exact artifact under test. Each attached evidence reference may bind to that same subject digest; mismatched subject digests are rejected.

A result's timestamps must be timezone-aware and ordered. A task completion event does not by itself mean a release is approved; release promotion remains gated by QA evidence and policy.

## 6. Workflow Event 2.0

Events include tenant/workflow/run identity, optional task identity and attempt, message/correlation/causation IDs, required trace context, event type, producer identity/version, optional sequence, occurrence timestamp, and payload.

Known lifecycle payloads are typed and validated. They carry the lease epoch and state version where the current communication ADR requires fencing. Policy decisions and approval decisions are explicit audit events. Validation verdicts bind evidence to an artifact digest.

Event consumers ignore unknown optional fields within the same major version. Unknown event types can be retained as generic events; known event types still validate their required payload fields. Credential-shaped fields are rejected in durable event payloads.

Consumers must treat delivery as at-least-once and deduplicate by event/message identity. Event ordering is not implied unless a producer supplies and maintains a sequence within its defined scope. The event model does not itself implement persistence, replay, transactional outbox/inbox, or an event broker.

## 7. AICode compatibility adapters

`rusa_agents/contract_adapters.py` provides explicit adapters:

- `coding_task_to_envelope` and `task_envelope_to_coding_task` map the current `CodingTask`.
- `worker_result_to_agent_result` and `agent_result_to_worker_result` map current worker results while preserving the exact six role identities.
- `task_record_to_snapshot` and `snapshot_to_task_record` preserve the current `TaskRecord` lifecycle state, worker results, evidence keys, errors, tenant, repository, and approval metadata.
- `queue_status_from_legacy` and `queue_status_to_legacy` preserve the existing PostgreSQL queue status values.

Only an explicit metadata allowlist crosses the adapter: tenant ID, approval-required flag, approval reference, and policy ID/version. Arbitrary metadata such as worker lease tokens is deliberately excluded from durable envelopes.

These adapters are migration utilities. They are not wired into the live AICode queue, worker runtime, or API path; existing runtime behavior remains unchanged.

## 8. Versioning and migration rules

- Additive optional fields may be introduced within the same major contract version.
- Consumers must ignore unknown optional fields within the same major version.
- Removing or reinterpreting a required field, changing a field type, or changing a state transition is a breaking change and requires a new major version.
- A major-version migration must publish a mapping, compatibility fixtures, producer/consumer test results, and a documented coexistence/deprecation window before the old version is retired.
- Existing A2A protocol v1 and AICode task/queue values remain supported during the migration window.

## 9. Validation boundary

Automated tests cover:

- Strict Agent Manifest handling, required health snapshot, published-version immutability, and lifecycle transitions.
- Tenant/task identity, timezone-aware deadlines, W3C trace context, bounded budgets, and approval metadata.
- Secret-shaped keys in durable payloads and unknown optional event fields.
- Typed task acceptance/progress/completion/failure/cancellation, policy, approval, and validation-verdict payloads.
- Immutable artifact/evidence digest binding and result timestamp ordering.
- AICode task, TaskRecord, six-worker-role, queue-status, evidence-key, and approval round-trip compatibility.
- JSON Schema validity, Pydantic/schema parity, and positive/negative payload fixtures.

The branch now includes separate PostgreSQL services and migrations for agent registry, workflow/run/task persistence, DAG scheduling primitives, transactional outbox/inbox, deterministic policy decisions, durable human approvals, and immutable evidence verification. These components are exercised by unit and PostgreSQL integration tests, but they are not yet wired into live AICode task intake or worker dispatch. No new production side effect is enabled by this increment; the current AICode queue, APIs, and six worker roles remain the compatibility baseline.
