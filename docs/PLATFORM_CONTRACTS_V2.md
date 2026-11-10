# RUSA AI Platform Contracts v2

**Status:** Initial contract implementation  
**Version:** Agent Manifest 1.0; Task, Result, and Workflow Event 2.0  
**Date:** 10 November 2026  
**Implementation:** \`rusa_agents/platform_contracts.py\`

## 1. Contract layers

RUSA AI now distinguishes three contract layers:

1. **Platform control-plane contracts** — the internal canonical identity and workflow models defined here.
2. **Domain execution contracts** — current AICode dataclasses and future domain-specific task/result payloads.
3. **Transport protocols** — HTTP/JSON today; the existing A2A wire envelope remains separately versioned.

The new \`TaskEnvelopeV2\` is not a replacement for the existing A2A protocol. The existing \`schemas/a2a/task-envelope.schema.json\` with \`protocol_version: 1.0\` remains unchanged. A future A2A adapter may map between the external A2A envelope and this internal task envelope; that adapter is not part of this increment.

## 2. Checked-in contracts

| Contract | Version | Schema |
|---|---:|---|
| Agent Manifest | 1.0 | \`schemas/platform/agent-manifest.v1.schema.json\` |
| Universal Task Envelope | 2.0 | \`schemas/platform/task-envelope.v2.schema.json\` |
| Agent Result | 2.0 | \`schemas/platform/agent-result.v2.schema.json\` |
| Workflow Event | 2.0 | \`schemas/platform/workflow-event.v2.schema.json\` |

All Pydantic models reject unknown fields. The checked-in JSON Schemas use JSON Schema Draft 2020-12 and are generated from the Pydantic models.

Regenerate schemas after changing a model:

\`\`\`bash
python scripts/export_platform_contract_schemas.py
\`\`\`

Verify that checked-in schemas match the models:

\`\`\`bash
python scripts/export_platform_contract_schemas.py --check
\`\`\`

CI runs the drift check before the test suite.

## 3. Agent Manifest 1.0

An agent manifest is a versioned declaration of identity, domain, capabilities, runtime binding, model policy, tool allowlist, tenant/resource scope, execution limits, approval policy, evaluation profile, and owner.

Important rules:

- \`agent_id\` is a stable lowercase identifier; \`version\` is semantic versioning.
- Published versions must be treated as immutable by the future registry.
- \`status\` distinguishes draft, validated, active, suspended, deprecated, and revoked.
- Runtime and policy profiles are required; a manifest is not a grant by itself.
- Tool permissions still require deterministic policy enforcement at the Tool Gateway.
- \`tenant_scope\` supports tenant, system, or explicit tenant allowlist scope.
- Deadlines and budgets are declared separately from model prompts.

The manifest model validates structure and timestamps. It does not register or activate an agent in a runtime; the registry API is a later backlog item.

## 4. Universal Task Envelope 2.0

Every internal platform task is bound to:

- \`tenant_id\`, \`workflow_id\`, \`run_id\`, and \`task_id\`.
- \`message_id\`, \`correlation_id\`, optional \`causation_id\`, and optional W3C \`traceparent\`.
- \`domain\`, \`agent_id\`, \`agent_version\`, and \`capability\`.
- Attempt, optional idempotency key, priority, deadline, and bounded execution budget.
- Input payload or immutable input references.
- Policy context and optional approval reference.

The task deadline must be timezone-aware and later than creation time. Budget values are hard-limit declarations for the scheduler/runtime to enforce; merely validating a task envelope does not enforce runtime limits.

Do not persist credentials in input, policy context, or event payloads. Credential-shaped keys such as \`api_key\`, \`access_token\`, \`authorization\`, \`password\`, \`client_secret\`, and \`private_key\` are rejected by Pydantic validation. Secrets must remain in a secret manager or a scoped tool service, not in task messages.

The legacy adapter uses a one-hour execution deadline and a 100-tool-call budget when no explicit budget is supplied. These are compatibility defaults for this adapter, not the final tenant policy.

## 5. Agent Result 2.0

An agent result is correlated to the tenant, workflow, run, task, agent version, capability, and attempt that produced it.

The result separates:

- \`output\` — structured output data.
- \`output_refs\` — optional content references.
- \`evidence_refs\` — immutable evidence references that require a SHA-256 digest.
- \`legacy_evidence\` — compatibility-only data from the current AICode WorkerResult.

**Legacy evidence is not a QA verdict.** Existing AICode evidence dictionaries are preserved in \`legacy_evidence\` during migration. A future QA service must produce digest-bound \`evidence_refs\` and a separate validation verdict before an artifact can be promoted.

Result timestamps must be timezone-aware and ordered.

## 6. Workflow Event 2.0

Events include tenant/workflow/run identity, optional task identity, message/correlation/causation IDs, trace context, event type, producer identity/version, optional sequence, occurrence timestamp, and payload.

Consumers must treat delivery as at-least-once and deduplicate by event/message identity. Event ordering is not implied unless a producer supplies and maintains a sequence within its defined scope. The event model does not itself implement persistence, replay, or an outbox.

## 7. AICode compatibility adapters

\`rusa_agents/contract_adapters.py\` provides explicit adapters:

- \`coding_task_to_envelope\` maps the current \`CodingTask\` to the internal v2 envelope.
- \`task_envelope_to_coding_task\` maps a Software Engineering \`coding.execute\` envelope back to the current task type.
- \`worker_result_to_agent_result\` maps \`WorkerResult\` to v2 while preserving legacy evidence as unverified data.
- \`agent_result_to_worker_result\` maps the result back to the current worker contract.

The task adapter copies only the authenticated tenant identifier from legacy metadata. It deliberately does not copy arbitrary metadata such as worker lease tokens into the durable envelope.

These adapters are migration utilities. They are not yet wired into the live AICode queue, worker runtime, or API path; existing runtime behavior remains unchanged in this increment.

## 8. Validation boundary

Automated tests cover:

- Strict/unknown-field handling and semantic version patterns.
- Tenant/task identity, timezone-aware deadlines, and bounded budgets.
- Secret-shaped keys in durable payloads.
- Immutable evidence digest format and result timestamp ordering.
- AICode task/result round-trip compatibility.
- JSON Schema validity, Pydantic/schema parity, and sample payload validation.

The contracts do not yet provide an agent registry, a workflow scheduler, transactional outbox/inbox, central approval service, or policy decision service. Those remain separate engineering backlog items.
