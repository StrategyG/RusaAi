# RUSA AI — Agentic DevOps integration mapping

**Status:** Architecture integration contract  
**Version:** 1.0  
**References:** ADR-COMM-001, RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE.md, and RUSA_AI_All_Agents.md

## 1. Authority and orchestration boundaries

The **Supreme Orchestrator** remains the system-wide coordinator and owns the user-facing workflow outcome. The **DevOps Orchestrator** is a domain coordinator invoked by the Supreme Orchestrator for software delivery lifecycle work. It coordinates the eight lifecycle stages but does not create a competing system-wide authority, independent tenant context, or second canonical workflow store.

For DevOps lifecycle runs, canonical state is the dedicated PostgreSQL
`devops_workflows` and `devops_tasks` store with transactional outbox/inbox,
introduced in Phase 3A (PR #66). This is a bounded DevOps workflow store, not
a replacement for AICode's existing `aicode_jobs` queue. Existing AICode
workers, API, and sandbox remain intact. The Supreme Orchestrator remains the
system-wide authority; the DevOps Orchestrator coordinates only the lifecycle
sub-workflow and cannot create a second tenant or policy authority.

## 2. Mapping of the ten system-wide agents

| System-wide agent | DevOps lifecycle relationship | Authority boundary |
|---|---|---|
| Supreme Orchestrator | Calls and supervises DevOps Orchestrator | Owns global user request and final report; cannot bypass Policy Engine or human approval |
| System Architect | Architecture advisor to Plan and cross-stage design review | Produces architecture decisions and constraints; does not own execution |
| Planner | Planning capability used by Plan Agent | Helps formulate plans; Plan Agent owns DevOps-stage contract and handoff |
| Task Decomposer | Produces/validates dependency DAG | Does not become another workflow-state authority |
| Software Engineer | Implementation capability used by Code Agent | Isolated feature branch/worktree; cannot merge or write protected branches |
| DevOps | Shared build, deployment, infrastructure and runbook capability | Executes only capabilities granted by assigned task |
| QA & Testing | Independent verification capability for Test Agent | Verdict bound to exact commit and artifact digest |
| Security & Policy | Security Guard analysis plus deterministic Policy Engine integration | Agent recommends and verifies; Policy Engine authorizes |
| Memory & Knowledge | Context and evidence retrieval | Retrieved content is untrusted evidence, not instruction or authority |
| Evaluation & Reporting | Reads workflow/evidence projections | Read-only to execution state; cannot invent PASS evidence |

The eight lifecycle agents are specialist stage identities, not replacements for all ten system-wide agents. DevOps Orchestrator is a domain coordinator beneath Supreme Orchestrator. Security Guard remains cross-cutting and is not a ninth stage.

## 3. Mapping to ADR-COMM-001 envelope

| DevOps field | ADR envelope field | Rule |
|---|---|---|
| workflow_id | run_id | One canonical workflow identifier; never generate a second ID |
| task_id | task_id | Stable across redelivery |
| tenant_id | tenant_id | Derived from authenticated context and checked against envelope |
| attempt | attempt | Required on task and result. Redelivery preserves attempt; retry creates a new attempt and lease. A result from an earlier attempt is rejected even when task ID is unchanged |
| audit.traceparent | traceparent | Propagate W3C trace context |
| handoff.next_agent | producer/routing metadata | Producer is authenticated sender; recipient derived from validated registry |
| handoff.event | type + domain event | Must match registered stage/event mapping |
| output.artifact_refs/evidence_refs | payload.result_ref/evidence refs | Immutable, tenant-scoped references |
| audit.source_commit | payload/evidence metadata | Exact revision built/tested |
| audit.artifact_digest | payload.sha256/artifact metadata | Immutable artifact digest, not a commit SHA |
| — | message_id | Unique logical event ID; unchanged on redelivery |
| — | correlation_id | Stable across one request/workflow |
| — | causation_id | References triggering event |
| — | idempotency_key | Stable for the same side-effecting operation across retries |

A valid task payload is not proof of authorization. Producer identity and tenant are derived from authenticated service context and checked before claim.

## 4. Lifecycle event mapping

The transport envelope remains generic with `type=task.completed`; `payload.stage` and `payload.domain_event` provide the lifecycle mapping.

| Stage | Domain event |
|---|---|
| Plan | `plan.completed` |
| Code | `code.completed` |
| Build | `build.completed` |
| Test | `test.completed` |
| Release | `release.approved` |
| Deploy | `deploy.completed` |
| Operate | `operate.incident_detected` or `operate.recovered` |
| Monitor | `monitor.alert_triggered` or `monitor.feedback_created` |
| Workflow blocked | `workflow.blocked` |
| Workflow complete | `workflow.completed` |

Consumers must reject a stage/domain-event mismatch and be idempotent. Release approval cannot be inferred from task completion; the approval record must exist.

## 5. Capability and evidence enforcement

`allowed_tools` contains canonical capability IDs. The schema allowlist is a contract guardrail, not an authorization grant. At execution time, Policy Engine and Tool Gateway must intersect authenticated agent identity, agent registry capabilities, requested task tools, write/network scopes, tenant/resource policy, approval, budget, deadline, and cancellation state.

Security Guard and Monitor are read-only identities. Empty `write_scope` does not make write-capable tools safe.

Successful Code results require a source commit. Successful Build, Test, Release, and Deploy results require source commit and immutable artifact digest. Build, Test, Release, and Deploy task inputs carry the expected source commit; Test, Release, and Deploy also carry the expected artifact digest. Result validation compares those values with originating task inputs. Evidence from another commit or digest cannot satisfy the current release gate.

## 6. Human approval boundaries

- Code Agent writes only to isolated feature branch/worktree.
- Merge to protected branches requires repository policy and human review.
- Release Agent prepares a candidate; publication follows release policy.
- Staging deployment may be automated when policy permits.
- Production deployment requires an approval reference and deploy scope.
- Agents receive no raw secret; secret access remains with a secret manager.
- Destructive operations, permission changes and production data changes require the relevant approval.

## 7. Implementation phases

| Phase | Scope | Exit evidence |
|---|---|---|
| 1 — Contract foundation | Schemas, capability IDs, stage mapping, cross-document validation | Positive/negative contract tests; no external side effects |
| 2 — Safe dry run | DevOps Orchestrator and dummy specialists | Synthetic Plan-to-Monitor workflow with audit evidence |
| 3A — Durable state | PostgreSQL workflow/task store, tenant isolation, transactional outbox/inbox | Idempotent intake, relay lease and RLS tests |
| 3B — RabbitMQ commands | Capability/priority routing, signed commands, confirms, manual ACK/NACK, DLQs, isolated P0 cancel | Routing, signature, retry, quarantine and outbox-settlement tests |
| 3C — Kafka event log | Tenant/run partitioning, signed event publishing, idempotent consumer projections | Publish-confirm, replay and duplicate-consumer tests |
| 3D — gRPC control plane | mTLS/SPIFFE identity, scoped DB roles, RPC deadlines, claim/lease/state fencing, cancellation | Identity, authorization, claim, heartbeat, result receipt and cancel tests |
| 3E — Reliability validation | Crash/restart, redelivery, replay, cancellation, lease expiry and side-effect reconciliation | CI evidence; production broker/worker provisioning remains a separate rollout gate |
| 4 — Specialist execution | Eight agents, per-agent identity, capability enforcement, independent QA | Staging candidate with exact commit/digest evidence |
| 5 — Operations and feedback | Operate/Monitor, SLO, alerts, budget, kill-switch, feedback | Security, SRE and product-owner sign-off |
