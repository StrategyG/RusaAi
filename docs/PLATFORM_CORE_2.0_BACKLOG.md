# RUSA AI Platform Core 2.0 — Engineering Backlog

**Status:** P0 control-plane foundations implemented on the architecture branch; runtime cutover and P1–P5 work remain open  
**Version:** 2.0-draft  
**Date:** 10 November 2026  
**Target repository:** StrategyG/RusaAi  
**Working branch:** architecture/rusa-ai-platform-core-2.0  
**Latest fully green CI commit:** `9e75790c98d372642a33d1da40cd5d89a0801d6c`  
**Current branch head:** `5d64652f2a726de7e455f9649c1a40c9edd5da28` (additional integration/security tests and fixes; full CI rerun still required)

This backlog translates the Platform Architecture 2.0 into sequenced engineering work. Items marked **P0** establish compatibility and shared control-plane foundations. Later domain work must not bypass these controls.

## 1. Delivery rules

- Preserve current AICode APIs, task contracts, database guarantees, and six worker roles while the platform contract evolves.
- Keep the current PostgreSQL queue as the initial durable execution mechanism.
- Add schema and migration changes incrementally; do not replace aicode_jobs in one migration.
- Every new runtime capability requires manifest, contract tests, policy scope, evidence, observability, and documentation.
- Do not enable new production side effects as part of the architecture-only phase.
- A green CI run validates only the exact commit and jobs executed; it is not production certification.

## 2. Implementation progress on the architecture branch

The branch now contains the contract foundation and the first code-bearing P0 control-plane implementation. These services and migrations are implemented as explicit platform components; they are **not yet wired into live AICode task intake or production worker dispatch**.

- [x] Canonical orchestrator hierarchy, communication ADR, and capability-status matrix.
- [x] Agent Manifest 1.0, Task Envelope 2.0, Agent Result 2.0, Workflow Event 2.0, Task Record snapshot 2.0, and Policy Decision 1.0 contracts with generated Draft 2020-12 JSON Schemas.
- [x] Backward-compatible AICode adapters and round-trip preservation tests for six worker roles, evidence, approval metadata, tenant/repository identity, and queue status values.
- [x] PostgreSQL workflow/run/task/dependency/event/outbox/inbox schema in migration 009, with tenant RLS, indexes, immutable event/dependency/inbox records, and atomic DDL.
- [x] DAG validation for missing dependencies, cycles, duplicate tasks/edges, fan-out, deadlines, and dependency outcomes.
- [x] PostgreSQL workflow store for run/task creation, compare-and-set state versions, lease fencing, bounded concurrent task claims, retries, deadlines, budget consumption, cancellation, side-effect reconciliation, and evidence-gated completion.
- [x] Transactional event/outbox append and at-least-once relay; tenant-aware inbox deduplication.
- [x] PostgreSQL versioned agent registry in migration 010 and service lifecycle for register, validate, activate, suspend, deprecate, revoke, health updates, capability selection, and runtime-binding checks.
- [x] Task-scoped capability authorization and an explicit Tool Gateway path that binds the policy resource to the actual tool arguments, worker role, tenant, agent version, task, resource scope, deadline, and deterministic decision.
- [x] Domain-orchestrator interface and AICode adapter; cross-domain handoff requires an explicit unexpired tenant grant.
- [x] Deterministic policy engine and append-only decision persistence, including allowlist precedence, deny, approval-required, stable identical denials, and canonical input digests.
- [x] Durable human approval records with exact action/resource/workflow/task/artifact binding, authorized human decisions, expiry, one-time consumption, and workflow events.
- [x] Immutable evidence records and QA verdict verification against exact artifact digest, commit, producer, evidence kind, test profile, and freshness.
- [x] PostgreSQL integration coverage for workflow RLS/outbox behavior and platform registry, policy, approval, and evidence services.

Still open before Platform Core 2.0 can be called end-to-end complete:

- Runtime wiring from authenticated API intake and the AICode orchestrator/worker lifecycle into the new registry, workflow store, policy service, approval service, evidence service, and authorized Tool Gateway path.
- Production-grade identity/approver authorization adapters, deployment configuration, migration runbook, backup/restore and rollback rehearsal, operational metrics/alerts, and owner sign-off.
- Versioned registrations and validated execution evidence for the named Supreme/Domain/DevOps and lifecycle agents. The supplied lifecycle identities remain planned until they pass the promotion rule in `AGENT_CAPABILITY_MATRIX.md`.
- P1 model/knowledge integration and the P2–P5 product-domain and enterprise-qualification workstreams.

CI evidence for commit `9e75790c98d372642a33d1da40cd5d89a0801d6c`:
- Main PostgreSQL-backed suite: 292 passed, 6 skipped.
- Isolated sandbox suite: 270 passed, 28 skipped (the isolated container does not receive the PostgreSQL integration DSN; the main suite runs those database tests).
- RAG/MCP integration suite: 45 passed.
- All three CI jobs passed, including schema drift, lint, Bandit, image builds, isolated tests, and Compose validation.
- Trivy reported 0 CRITICAL and 67–72 HIGH findings per image. The existing `--ignore-unfixed` gate passed; the HIGH findings remain a base-image/dependency disposition and refresh follow-up, not a clean scan.

Follow-up validation after that green commit:
- A PostgreSQL-backed service integration test was added for registry lifecycle/selection, policy decision persistence, self-approval rejection, one-time approval consumption, evidence-kind/provenance verification, and governance-table RLS/grants.
- CI run #4141 exposed an untyped NULL in the optional registry domain filter; fixed with an explicit PostgreSQL text cast.
- CI run #4148 stopped at Ruff import-order validation before tests; imports were moved to the module header in commit `5d64652f2a726de7e455f9649c1a40c9edd5da28`.
- The later commits have not yet produced a complete green CI run. Manually dispatch workflow `RUSA-AI CI` against the current branch head before merge; do not use the earlier green run as evidence for the current head.

## 3. Workstream overview

| Priority | Workstream | Outcome |
|---|---|---|
| P0 | Architecture and naming alignment | One canonical hierarchy and explicit current/target boundary |
| P0 | Agent and workflow contracts v2 | Versioned manifests, tasks, results, events, and evidence references |
| P0 | Durable workflow control plane | Cross-domain workflow/run/task state without breaking AICode queue |
| P0 | Agent registry foundation | Capability discovery, versioning, and runtime registration |
| P0 | Policy and approval contract | Deterministic enforcement and explicit human decision records |
| P1 | Software Engineering lifecycle | Eight-stage lifecycle integrated with existing coding workers |
| P1 | Model and knowledge integration | Shared provider/model/knowledge access with clear service ownership |
| P2 | RUSA Intelligence | Research, data, knowledge, and decision-support workflows |
| P3 | RUSA Business | Governed business operations and automation |
| P4 | RUSA Ecosystem | Developer platform, agent builder, integrations, and marketplace |
| P5 | Enterprise qualification | Operational evidence and production certification |

## 4. P0 — Architecture and naming alignment

<badge>Completed on this branch: ADR-PLATFORM-001/002 and AGENT_CAPABILITY_MATRIX.md</badge>

### CORE-001 — Canonical orchestrator hierarchy

**Deliverable:** ADR defining Supreme Orchestrator, Domain Orchestrator, DevOps Orchestrator, and AICodeOrchestrator boundaries.

Acceptance criteria:

- Supreme Orchestrator is the only proposed global cross-domain coordinator identity.
- AICodeOrchestrator remains an implementation class for the current coding workflow.
- DevOps Orchestrator is scoped to Software Engineering lifecycle coordination.
- Executive Orchestrator in the communication ADR is explicitly reconciled rather than silently duplicated.

### CORE-002 — Communication ADR reconciliation

**Deliverable:** ADR update that distinguishes implemented transport from proposed transport.

Acceptance criteria:

- PostgreSQL queue/leases are documented as the current durable task mechanism.
- RabbitMQ and Kafka remain deferred until load, replay, fan-out, or isolation requirements justify adoption.
- Transactional outbox/inbox is evaluated as the first event reliability step.
- WebSocket is not described as implemented; any future stream must define cursor, reconnect, replay, and authorization.

### CORE-003 — Capability status registry

**Deliverable:** a maintained capability matrix used by documentation and Control Center labels.

Acceptance criteria:

- Current runtime roles, target agent catalogue, and planned domain capabilities are separate states.
- No UI surface labels a planned agent as active without a runtime registration and execution evidence.
- The matrix records owner, status, contract version, tests, and evidence link.

## 5. P0 — Agent and workflow contracts v2

### CORE-010 — Agent manifest schema

**Deliverable:** JSON Schema Draft 2020-12 and Pydantic model for an agent manifest.

Required logical fields:

- schema_version, agent_id, domain, version, status.
- capabilities, input_schema_ref, output_schema_ref.
- runtime_binding, model_policy, tool_allowlist.
- tenant_scope, resource_scope, deadline_policy, budget_policy.
- policy_profile, approval_policy, evaluation_profile.
- health, owner, created_at, updated_at.

Acceptance criteria:

- Unknown fields are rejected for the initial strict contract.
- Agent IDs and versions are immutable once published.
- Registration fails when capability, schema, policy, or runtime binding is missing.
- Registry status can distinguish draft, validated, active, suspended, deprecated, and revoked.

### CORE-011 — Task, result, and event schemas

**Deliverable:** versioned schemas for task request, task acceptance, progress, completion, failure, cancellation, policy decision, approval, and validation verdict.

Required task identity and control fields:

- Tenant, workflow, run, and task identifiers.
- Message, correlation, and causation identifiers.
- W3C trace context.
- Domain, agent ID, agent version, and capability.
- Attempt, idempotency key, deadline, priority, and budget.
- Immutable input, output, and evidence references.
- Policy context and approval reference where applicable.

Acceptance criteria:

- JSON Schema Draft 2020-12 validation is covered by positive and negative fixtures.
- Event consumers can safely ignore unknown optional fields within the same major version.
- Breaking changes require a new major schema version and a documented migration window.
- Raw secrets and bearer credentials are rejected or excluded from durable envelopes.

### CORE-012 — Backward-compatible AICode adapter

**Deliverable:** adapter between current CodingTask, WorkerResult, TaskState, and the v2 task/result contracts.

Acceptance criteria:

- Current AICode API payloads remain valid.
- The six existing worker roles retain their exact identities.
- Existing evidence keys and current queue status values remain readable during migration.
- Contract tests prove round-trip conversion without losing tenant, repository, task, evidence, or approval information.

## 6. P0 — Durable workflow control plane

### CORE-020 — Workflow data model

**Deliverable:** migration proposal and schema for workflow-level state.

Candidate entities (final names to be approved in the migration design):

- workflow_runs — tenant, objective, state, deadline, budget, and final report reference.
- workflow_tasks — agent/capability assignment, task state, attempts, lease, and evidence references.
- workflow_dependencies — validated directed edges between tasks.
- workflow_events — versioned lifecycle and policy events.
- workflow_outbox / inbox deduplication records — reliable publication and idempotent consumption.

Acceptance criteria:

- Existing aicode_jobs remains supported and can be adapted to the generalized task model.
- Every table has tenant-scoped access controls, required indexes, and migration/rollback notes.
- Workflow DAG validation rejects cycles, missing dependencies, cross-tenant edges, and invalid fan-out.
- State transitions use optimistic concurrency or compare-and-set versions.

### CORE-021 — Workflow scheduler and DAG executor

**Deliverable:** scheduler that claims only tasks whose dependencies and policy gates are satisfied.

Acceptance criteria:

- Bounded concurrency, retry, repair, deadline, and cost budgets are enforced outside the model.
- A failed dependency blocks downstream tasks and records the reason.
- Worker crash, lease expiry, duplicate delivery, and cancellation are recoverable.
- A replay or reporting consumer cannot invoke tools or create side effects.

### CORE-022 — Transactional event publication

**Deliverable:** outbox relay and idempotent event consumer interface using the existing PostgreSQL boundary.

Acceptance criteria:

- State change and outbox event commit atomically.
- Publication retries do not duplicate logical events.
- Consumer inbox/deduplication is tenant-aware.
- Event schema version, timestamp, producer, correlation, causation, and trace context are validated.
- Metrics expose outbox age, publish failures, consumer lag, redelivery, and dead-letter counts.

## 7. P0 — Agent registry and execution

### CORE-030 — Registry API and runtime binding

**Deliverable:** register, validate, inspect, activate, suspend, deprecate, and revoke agent definitions.

Acceptance criteria:

- A registered agent is bound to a runtime implementation and an immutable version.
- Runtime startup rejects unregistered or revoked agent versions.
- Agent selection is based on declared capabilities and tenant/policy scope, not free-form model naming.
- A registry change is audited and cannot grant a tool permission by itself.

### CORE-031 — Capability-based tool authorization

**Deliverable:** map manifest capability and task-scoped grant to the existing role allowlist and Tool Gateway enforcement.

Acceptance criteria:

- The Tool Gateway remains the final enforcement point.
- Model prompts are not treated as authorization controls.
- Every invocation is checked against tenant, agent version, task, tool, resource, deadline, and policy.
- High-risk and protected actions require approval or are denied.

### CORE-032 — Domain orchestrator interface

**Deliverable:** common interface for plan, dispatch, progress, cancel, validation, and final report.

Acceptance criteria:

- The current AICodeOrchestrator is wrapped/adapted, not replaced in the first increment.
- Domain orchestrators cannot access another domain's tools or data without an explicit cross-domain grant.
- Supreme Orchestrator can coordinate domain runs but cannot override domain or platform policy.

## 8. P0 — Policy, approval, and evidence

### CORE-040 — Policy decision contract

**Deliverable:** versioned allow/deny/approval decision schema and deterministic decision interface.

Acceptance criteria:

- Every decision includes actor, tenant, action, resource, policy version, outcome, reason code, and timestamp.
- Denials are terminal for the same action unless policy inputs change.
- Model output cannot create or alter policy decisions.

### CORE-041 — Human approval records

**Deliverable:** backend contract for request, approve, reject, expire, and audit approval.

Acceptance criteria:

- Approval binds to exact action, resource, tenant, task/workflow, and artifact digest when applicable.
- Approval expires and cannot be replayed for a different action or artifact.
- The current UI does not expose approve/reject until this API and authorization model exist.

### CORE-042 — Evidence binding

**Deliverable:** immutable evidence reference contract with content digest, producer, subject commit/artifact, test profile, and timestamp.

Acceptance criteria:

- QA verdict references the exact artifact digest being promoted.
- Missing, stale, or mismatched evidence blocks completion or release.
- Audit events retain evidence references and digest values.

## 9. P1 — Software Engineering lifecycle

Implement the eight lifecycle identities in the supplied DevOps specification as a staged migration:

1. Plan Agent — translate requirements into task plans and acceptance criteria.
2. Code Agent — implement changes in task-scoped workspaces.
3. Build Agent — create reproducible artifacts and manifests.
4. Test Agent — run approved test profiles and produce evidence.
5. Release Agent — assemble release candidate and release manifest.
6. Deploy Agent — execute approved deployment workflow.
7. Operate Agent — manage runbooks and incidents within policy.
8. Monitor Agent — collect SLO, logs, traces, and feedback.

Acceptance criteria:

- Each stage has its own agent ID, manifest, contract, tool policy, and evaluation.
- Existing Developer/Debugger/Tester/Security roles are reused only through explicit adapters.
- Merge, release promotion, production deployment, infrastructure mutation, and secret changes remain human-gated.
- Monitor feedback creates a new workflow request; it does not silently mutate production.

## 10. P1 — Model and knowledge integration

### MODEL-001 — RusaModels API client

**Deliverable:** typed authenticated client for model catalog, versions, artifact metadata, job status, and configured inference proxy.

Acceptance criteria:

- RusaAi receives no RusaModels database or object-store credentials.
- Service-token rotation, timeout, retries, and error mapping are tested.
- The client does not assume that a model is loaded or supports tool calling based only on dependency health.

### KNOW-001 — Shared knowledge policy

**Deliverable:** domain-aware retrieval grants over the existing tenant-scoped RAG service.

Acceptance criteria:

- Retrieval enforces tenant, collection, role, domain, and document ACL.
- Retrieved content is treated as untrusted data, not instructions.
- Research outputs preserve source references and retrieval provenance.

## 11. P2 — RUSA Intelligence

Candidate first capabilities:

- Research Agent.
- Data Intelligence Agent.
- Knowledge Intelligence Agent.
- Market/Competitive Intelligence Agent.
- Strategic Analysis and Decision Support.

Acceptance criteria:

- Each capability has a registered manifest and evaluation dataset.
- Research claims distinguish source-backed facts, inference, and uncertainty.
- Data access is read-only by default; writes and external actions require policy grants.
- Retrieval quality and groundedness are measured before activation.

## 12. P3 — RUSA Business

Candidate first capabilities:

- Business Analyst and Operations Agent.
- Sales and Marketing Agent.
- Finance and Accounting Agent.
- Customer Success Agent.

Acceptance criteria:

- Business workflows are tenant- and organization-scoped.
- Financial and legal side effects require explicit human approval.
- CRM, accounting, email, and other connectors use scoped credentials through Tool Gateway.
- Every business action has an operation ID, audit record, and reconciliation path.

## 13. P4 — RUSA Ecosystem

Candidate platform surfaces:

- Agent Builder and local validation environment.
- Developer API and SDK.
- Agent Registry publishing workflow.
- Marketplace discovery and installation.
- Integration Hub and partner controls.

Acceptance criteria:

- Third-party agents are signed/versioned, sandboxed, evaluated, permission-scoped, and revocable.
- Marketplace publication does not imply permission to run sensitive tools.
- Tenant admins control installation, data access, and allowed capabilities.
- Usage, costs, version changes, and deprecation are observable.

## 14. P5 — Enterprise qualification

Use the existing production-certification contract and its evidence-bound decision model. Do not create a parallel certification mechanism.

Required qualification includes exact-commit release integrity, supply-chain evidence, vulnerability disposition, tenant isolation, penetration testing, backup/restore, disaster recovery, rollback, observability, SLO, staging soak, independent QA, and required owner sign-offs.

Acceptance criteria:

- Certification decision is bound to immutable release manifest digest and exact source commit.
- Missing evidence produces HOLD; mandatory failure produces FAIL.
- No documentation or UI claims production-ready before an approved PASS decision.

## 15. P0 implementation increment — code-bearing foundation on this branch

The branch includes the following implementation increment:

1. Versioned Pydantic contracts and generated JSON Schemas, plus backward-compatible AICode adapters.
2. Migrations 010 and 011 for workflow control-plane and governance persistence.
3. PostgreSQL workflow store, DAG validator, transactional outbox/inbox, and bounded task scheduler primitives.
4. Agent registry, capability authorization, Tool Gateway authorization path, and domain-orchestrator adapter.
5. Deterministic policy decision service, durable human approvals, one-time approval consumption, and immutable evidence verification.
6. Contract, governance, workflow, migration, and PostgreSQL integration tests.

**Integration boundary:** the new platform services are available as separate components, but they are not yet the live execution path for existing AICode intake and worker dispatch. No Supreme Orchestrator or eight lifecycle agents are promoted to active runtime identities by this increment. Existing AICode APIs, six worker roles, and queue remain the compatibility baseline.

**Migration safety:** migrations 010 and 011 are additive and leave `aicode_jobs` intact. They contain append-only event/evidence/audit data; do not perform a destructive down-migration. Any rollback must use a reviewed backup/restore or forward-fix plan, with owner approval and a rehearsal against a representative database before production use.

**Validation evidence:** see Section 2 for exact CI commit and suite counts. CI success is not production certification; the remaining HIGH image findings and the open runtime/operational qualification items must be dispositioned separately.

## 16. Definition of done for Platform Core 2.0

- A versioned agent can be registered, validated, activated, suspended, and revoked.
- A cross-domain workflow can be planned as a validated DAG and executed with bounded task ownership.
- Existing AICode tasks continue to work through compatibility adapters.
- All actions are tenant-scoped and enforced through deterministic policy and Tool Gateway checks.
- Retry, cancellation, lease loss, replay, and worker recovery have automated tests.
- Completion and release are bound to QA evidence for exact artifact digests.
- Control Center reports only actual runtime capabilities.
- RusaModels remains a separate authenticated service boundary.
- Production certification status remains truthful and evidence-based.
