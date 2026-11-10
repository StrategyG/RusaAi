# RUSA-AI DevOps Agent Contract Foundation

**Status:** Implemented contract baseline; agent execution remains staged.

This implementation follows the proposed eight-stage lifecycle and the communication ADR while retaining the existing AICode contracts. It does not replace `CodingTask`, `WorkerResult`, the AICode worker DAG, or the current PostgreSQL task queue.

## Contract files

- `schemas/devops/agent-task.schema.json` — task identity, workflow/tenant binding, stage, input references, permission constraints, priority, deadline, retry budget, and idempotency key.
- `schemas/devops/agent-result.schema.json` — task-bound result, artifacts, evidence, findings, unresolved issues, side-effect status, approval request, and audit metadata.
- `schemas/devops/workflow-event.schema.json` — versioned lifecycle/event envelope with message ID, workflow/run ID, tenant, correlation, W3C trace context, producer, timestamp, and event-specific payload requirements.

All schemas use JSON Schema Draft 2020-12 and reject unknown fields. The event envelope carries both `workflow_id` (lifecycle terminology) and `run_id` (communication ADR terminology); the validator requires them to be identical.

## Agent identity and permissions

`rusa_agents/devops_contracts.py` declares the ten general-purpose agent identities, the eight lifecycle agents, the DevOps orchestrator, and the cross-stage Security Guard. Each profile records capabilities, allowed tools/actions, read/write scopes, network permission, and runtime implementation status.

Protected actions are never authorized by an agent profile. They return `requires_human_approval=true`, including protected-branch merge, branch-protection changes, staging/production deployment, risky migration, resource deletion, secret/identity/security-policy changes, budget override, and private-asset publishing.

The registry is a permission contract, not proof that every named agent has a running executor. `runtime_status=partial` identifies responsibilities already represented by the current AICode workers; `contract_only` identifies agents that still need implementation.

## Validation

```python
from rusa_agents.devops_contracts import (
    validate_agent_task,
    validate_agent_result,
    validate_workflow_event,
    validate_workflow_handoff,
)
```

Validation checks JSON Schema, registered agent identity, stage ownership, tool/write-scope/network ceilings, bounded attempts, timezone-aware deadlines, tenant/workflow/task binding, event producer, and evidence references. A passed stage requires evidence. A workflow handoff cannot cite evidence that is absent from the result.

The test suite includes a dummy Plan Agent contract dry-run. It produces only synthetic references and has no tool gateway, repository write access, deployment client, or production credentials.

## Durable queue mapping

The existing `PostgresTaskQueue` remains the only durable task queue. Migration `009_aicode_queue_partitions.sql` adds `queue_name` and a claim index to the existing tenant-scoped table. AICode APIs and workers default to `queue_name="aicode"`; a future DevOps worker must explicitly use `queue_name="devops"` for enqueue, claim, lease renewal, authorization, and acknowledgement. Queue-name filtering is enforced on claim and lease operations, so the AICode worker cannot claim or acknowledge DevOps jobs. The existing tenant RLS policies remain in force. No RabbitMQ, Kafka, or second PostgreSQL queue is introduced. DevOps enqueue remains disabled until a dedicated DevOps worker and API adapter are implemented.

## Not implemented by this phase

- Dedicated Plan/Build/Release/Deploy/Operate/Monitor executors.
- Durable DevOps workflow state machine, transactional outbox/inbox, or broker relay.
- gRPC/Protobuf agent RPC, RabbitMQ command queues, Kafka event log, JWS message signing, mTLS, or event replay.
- Human approval service or deployment/merge capability.
- Production qualification and end-to-end staging evidence.

Those remain subsequent phases; schema validation alone is not production certification.
