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
- Phase 3A (PR #66) adds the dedicated PostgreSQL DevOps workflow/task
  store, transactional outbox/inbox, tenant-scoped idempotency, and relay
  lease fencing. The existing AICode `aicode_jobs` queue remains unchanged.
- Phase 3B/3C/3D (PR #67) add RabbitMQ command transport, Kafka event-log
  adapters, Ed25519 detached-JWS signing, and the internal mTLS/SPIFFE
  WorkflowControl gRPC service with task claims, lease heartbeats, state
  fencing, idempotent RPC receipts, and cancellation epochs.
- These are durable transport/control-plane primitives. A production
  RabbitMQ/Kafka cluster, deployed relay process, and fully automated
  eight-stage worker fleet are not provisioned by PR #67.

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

## Phase 2 — safe dry-run workflow (implemented in PR #65)

- Implemented the in-process DevOpsDryRunOrchestrator in
  rusa_agents/devops/orchestrator.py using SyntheticDevOpsAgent fixtures only.
- Uses the canonical task/result/event contracts from Phase 1 and validates
  every generated task, result, and emitted event.
- Runs Plan → Code → Build → Test → Release → Deploy → Operate → Monitor
  against synthetic repository and service references.
- Runs the cross-cutting Security Guard as a read-only review after each
  successful specialist stage.
- Requires approval references for Release and Deploy; a production Deploy
  task is not created before its approval reference is supplied. Merge,
  secret/permission mutation, destructive runbook execution, and all other
  external capabilities remain unavailable because dry-run task capabilities
  are empty, network access is disabled, and write scopes are empty.
- Failed, blocked, invalid, cancelled, skipped, and approval-pending outcomes
  stop downstream stages with the corresponding audit/workflow event.
- Monitor feedback returns a causally linked task.request event for a new
  Plan workflow; it does not publish that event to a queue.

**Exit criteria:** one full synthetic workflow completes with auditable
evidence and no external side effects.

**Validation:** CI run
[42400222667](https://github.com/StrategyG/RusaAi/actions/runs/42400222667)
passed all three jobs on head e0f4a90fbc42a847c0b0295132c8344b18476cae.
The core test suite reported 200 passed, 5 skipped, and 1 warning; RAG/MCP
integration tests passed; the isolated sandbox test suite, production image
build/scan gate, and production Compose validation passed. A passing Trivy
gate is not a zero-vulnerability claim; unfixed HIGH findings still require
package-level triage. This phase is a simulation, not production certification.

## Phase 3 — durable workflow and inter-agent communication

**Implemented in the stacked PR #66/#67 sequence:**

- **3A — canonical state:** PostgreSQL workflow/task tables, tenant isolation,
  idempotent intake, transactional outbox/inbox, and relay lease fencing.
- **3B — commands:** RabbitMQ capability/priority routing, isolated P0
  cancellation queue, signed persistent messages, publisher confirms, manual
  acknowledgements, bounded redelivery, and per-queue DLQs.
- **3C — event log:** Kafka lifecycle/policy/audit publishing, tenant/run
  partitioning, authenticated consumers, and replay-safe projections.
- **3D — control plane:** mTLS/SPIFFE workload identity, scoped API/worker
  database credentials, RPC deadlines, task claim IDs, lease epochs,
  heartbeats, state-version fencing, idempotent progress/result receipts,
  and monotonic cancellation epochs.
- **3E — reliability validation:** CI covers redelivery, replay, cancellation,
  lease expiry, tenant isolation, partial failure, and side-effect
  reconciliation.

The implementation deliberately does not provision a production broker
cluster, start a production outbox relay/worker pool, or claim that all eight
specialist agents are fully automated. A task consumer must obtain an accepted
durable claim before executing or acknowledging a task request.

**Exit criteria:** crash/restart, redelivery, replay, cancel, and partial
failure tests preserve canonical workflow state without duplicate effects.
Production rollout still requires broker operations, workload certificate/key
rotation, queue/topic permissions, DLQ monitoring, and worker-fleet integration.

## Phase 4 — specialist execution and QA

**Phase 4A (PR #68) establishes the specialist authority boundary:**

- Adds a fail-closed registry for all eight lifecycle specialist identities
  plus the cross-cutting Security Guard.
- Defines per-agent tool allowlists and stage-specific write scopes.
- Keeps Monitor and Security Guard read-only and denies arbitrary network
  access from every specialist by default.
- Enforces capability policy in gRPC GetTask/ClaimTask and repeats it inside
  the PostgreSQL row-lock transaction before a lease is created.
- Requires an explicit approval reference for production Deploy tasks.

**Phase 4B (this increment) implements Plan and Code execution:**

- Plan Agent validates a structured plan and persists five tenant-scoped,
  content-addressed planning artifacts plus provenance evidence.
- Code Agent requires an accepted claim and live lease/cancellation
  checkpoints, then works in a task/attempt/claim-scoped Git worktree.
- The Code tool gateway enforces actual file paths against task write scopes
  and publishes only a feature branch plus draft pull request.
- Source commit, changed-file hashes, branch and PR are recorded as immutable
  evidence. Ambiguous publication outcomes are not silently retried.

**Phase 4C (this increment) implements Build and independent QA adapters:**

- Build Agent accepts only an immutable source commit and a content-addressed build profile. The injected builder must perform two clean builds and return matching OCI digests.
- Build Agent validates the OCI registry host against a host-side allowlist, validates SPDX or CycloneDX SBOM JSON, and emits detached-signed build and SBOM attestations.
- Test Agent requires the signed Build evidence reference, verifies both Build and SBOM signatures, and checks tenant, workflow, source commit, artifact digest, registry, and SBOM digest before running tests.
- Test Agent accepts only named test suites from an offline test plan. Required suites must pass; required skipped suites and any failed suite block release.
- Test reports and attestations are immutable, tenant-scoped, and signed by the independent Test Agent identity. A passing result is bound to the exact source commit and artifact digest.

**Phase 4D (this increment) implements Release candidate preparation:**

- Release Agent verifies detached-signed Build, SBOM, and independent Test evidence.
- It checks tenant/workflow identity, exact source commit and artifact digest, SBOM checksum, required test suites, changelog, migration notes, and optional signed Security Guard evidence.
- A host-injected approval verifier enforces a release approval when the task policy requires it.
- It creates an immutable signed release-candidate manifest and readiness report. It does not publish an official release or deploy an environment.

**Phase 4E (this increment) implements staging-only deployment:**

- Deploy Agent verifies the detached-signed Release attestation and content-addressed candidate manifest against the exact tenant, workflow, source commit, OCI digest, and registry allowlist.
- Only host-configured staging target IDs and service names are accepted; the agent task has no raw network access, endpoint, command, or credential fields.
- Host policy can require a human approval bound to the deployment stage, tenant, workflow, commit, and artifact digest.
- Before deployment, the adapter inspects the active digest and rehearses rollback to the exact previous digest in the rollback plan.
- Deployment must confirm the exact candidate digest; host-injected health and named smoke checks must observe that digest before the Deploy result passes.
- The immutable deployment record and detached-signed Deploy attestation are stored in the tenant content-addressed artifact store.
- Optional automatic rollback is disabled by default and can only be enabled by host policy; when enabled, restoration is checked against the previous digest.

**Remaining Phase 4 work:**

- Integrate Operate, Monitor, and Security Guard with allowlisted services and telemetry.
- Provision production BuildKit/test services, worker pools, heartbeat loops, and live child-task orchestration.

The detailed boundaries and follow-on increments are documented in
[PHASE4_SPECIALIST_EXECUTION.md](PHASE4_SPECIALIST_EXECUTION.md).

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
