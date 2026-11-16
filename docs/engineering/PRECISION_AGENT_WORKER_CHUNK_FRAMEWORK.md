# Precision Agent/Worker Chunk Execution Framework

**Status:** PROPOSED / DOCUMENTATION ONLY  
**Target repository:** `StrategyG/RusaAi`  
**Baseline:** `main` at `1adac8d340e5870570910168b6626fe285dd2cf2`  
**Activation:** NOT AUTHORIZED  
**Production claim:** NONE

## 1. Purpose

Define a repeatable, evidence-driven way for RusaAi agents and workers to decompose engineering goals into bounded chunks, build or repair them, compare alternative solutions, and verify the result without losing task context or overstating completion.

This document is an execution contract and roadmap. It does not implement or activate a new runtime, worker role, tool grant, database migration, model, deployment, or production workflow.

## 2. Non-negotiable invariants

1. **Agent != authority.** An agent proposes and performs only actions authorized by host policy and its registered capabilities.
2. **LLM != validator.** Model self-assessment is never acceptance evidence.
3. **Plan != execution.** A plan or dry-run cannot be reported as a completed change.
4. **Chunk != arbitrary prompt.** Every chunk has one outcome, explicit inputs, bounded scope, dependencies, owner, and acceptance tests.
5. **Worker != unrestricted executor.** Workers receive least-privilege capabilities and task-scoped paths/resources.
6. **Evidence binds to exact inputs.** Results identify repository, base/head commit, task/chunk ID, changed paths, test command/profile, exit status, and report digest where available.
7. **No silent fallback.** Missing provider, tool, sandbox, credentials, approval, or test environment produces BLOCKED / NOT VERIFIED, not simulated success.
8. **Human approval remains authoritative** for protected branch writes, merge, release, deployment, secret/permission changes, destructive operations, and other policy-defined high-risk actions.
9. **No duplicate control planes.** Reuse the canonical queue, lease, policy, event, and evidence stores. Do not introduce a parallel queue or bypass existing tenant isolation.
10. **No cross-project inference.** Tenant, repository, project, agent identity, and write scope come from authenticated host context, never model-generated fields.

## 3. Agent and worker responsibility model

| Role | Responsibility | Must not do |
|---|---|---|
| Supreme Orchestrator | Decompose goal, build dependency DAG, assign registered capabilities, monitor state | Self-approve or fabricate evidence |
| Product/Problem Analyst | Clarify desired outcome, constraints, measurable acceptance criteria | Convert assumptions into facts |
| Solution Architect | Inspect current architecture, contracts, ADRs, migration graph, integration boundaries | Rewrite architecture without an ADR/change approval |
| Research Agent | Find source-grounded alternatives, compatibility and trade-off evidence | Treat web/model output as verified implementation |
| Plan Agent | Produce atomic chunks, estimates, dependencies, rollback and verification plans | Execute code or grant capabilities |
| Code Agent | Implement approved chunk within granted paths and task scope | Modify protected files outside allowlist |
| Repair Agent | Reproduce defect, isolate cause, implement minimal repair | Hide failing tests or weaken assertions to pass |
| Alternative Solver | Produce at least two viable options for material design choices; compare cost/risk | Change selected approach without decision record |
| Build Worker | Run fixed build profiles in isolated executor | Run arbitrary task-supplied shell/network commands |
| Test/QA Agent | Independently verify acceptance criteria against exact candidate commit | Approve its own implementation evidence |
| Security Guard | Review permissions, tenant isolation, secrets, supply chain and attack surface | Perform product implementation or grant access |
| Integrator | Reconcile compatible chunk outputs and dependency order | Resolve conflicting migrations by renumbering silently |
| Release/Deploy/Operate | Prepare and verify controlled release lifecycle | Bypass approval or deploy from unverified evidence |

A role in this table is a target responsibility, not proof that a production executor is registered or active. Runtime activation must be evidenced by the live registry, host adapter configuration, policy decision, and successful integration test.

## 4. Chunk contract (required fields)

Each chunk must be represented as a versioned, schema-validated object with these logical fields:

- `chunk_id`: stable unique ID, e.g. `RUSA-ENG-CHUNK-004`.
- `contract_version`: explicit schema version.
- `goal`: one measurable outcome.
- `problem_statement`: observed behavior and evidence; distinguish facts from hypotheses.
- `scope.included` / `scope.excluded`: exact paths, modules, APIs, migrations, and non-goals.
- `inputs`: immutable commit SHA, source references, issue/ADR IDs, artifacts and their digests.
- `dependencies`: predecessor chunk IDs and required output versions.
- `owner_role` and `worker_profile`: registered identities only.
- `capabilities`: explicit read/write/test/build/network permissions; default deny.
- `acceptance_criteria`: observable, binary checks with thresholds and expected artifacts.
- `verification_plan`: independent test/security/review procedure.
- `risk_level`, `budget`, `deadline`, `retry_policy`: bounded controls.
- `alternative_analysis`: options, evidence, trade-offs, selected option and decision owner.
- `rollback`: safe revert/forward-fix procedure and data implications.
- `status`, `attempt`, `lease`, `checkpoint_version`: runtime-managed fields.
- `evidence_refs`: immutable references bound to the exact commit and chunk.
- `human_approval_ref`: required only where policy says so; must bind to exact action and digest.

Reject unknown contract versions, missing required fields, invalid dependency graphs, path escape, unbounded budgets, and capability requests not granted by host policy.

## 5. Chunk lifecycle and state semantics

Recommended lifecycle:

`DRAFT -> TRIAGED -> PLANNED -> READY -> CLAIMED -> RUNNING -> VERIFYING -> PASSED`

Non-success terminal or waiting states:

- `BLOCKED`: prerequisite, capability, environment, or approval is unavailable.
- `FAILED`: execution failed after bounded retries or a non-retryable error.
- `NEEDS_REPAIR`: verification found a reproducible defect; create a repair chunk linked to the failed chunk.
- `NEEDS_DECISION`: alternatives have materially different consequences and an authorized decision owner is required.
- `CANCELLED`: cancellation was accepted and active work was stopped/fenced.
- `SUPERSEDED`: replaced by a newer chunk/contract; retain history.

Only the independent verifier may emit `PASSED`, and only when all mandatory acceptance checks pass on the exact candidate SHA. A worker exit code of zero alone is insufficient.

## 6. Orchestration algorithm

1. **Intake:** authenticate principal; derive tenant and repository from trusted context; assign idempotency key and root task ID.
2. **Baseline:** record clean/dirty worktree equivalent, base SHA, open PR/branch conflicts, current CI, dependency and migration inventory. If baseline cannot be established, stop with BLOCKED.
3. **Discover:** inspect source, tests, contracts, ADRs, configuration and operational docs. Cite file paths and line ranges in the internal evidence manifest.
4. **Decompose:** create a DAG of small chunks. A chunk should have one owner and one independently testable output. Split work when files, responsibilities, or acceptance gates conflict.
5. **Plan review:** Architect + Security review scope, dependency order, data/tenant boundaries, rollback and required approvals.
6. **Claim:** worker atomically claims an eligible task with tenant/task/worker identity, lease token, expiry, attempt count and checkpoint version.
7. **Execute:** worker checks lease before every protected operation; write only within granted paths; emit structured progress and checkpoints.
8. **Verify:** separate QA worker fetches exact candidate SHA, runs fixed tests and security profiles, and stores immutable evidence.
9. **Repair loop:** failed checks create a linked repair chunk with failing assertion, reproduction steps, root-cause hypothesis, minimal patch scope and regression test. Retry only transient failures under a bounded policy.
10. **Integrate:** integrate in topological order; rebase/merge conflicts create a reconciliation chunk, never overwrite another worker's changes.
11. **Approval gate:** protected actions require a distinct human approver and one-time approval bound to tenant, repo, action, target ref and exact commit/artifact digest.
12. **Close:** mark PASSED only with complete evidence; otherwise retain BLOCKED / FAILED / NEEDS_DECISION and the exact reason.

## 7. Concurrency, lease and recovery rules

- At-least-once delivery is assumed; handlers must be idempotent.
- Claims use atomic compare-and-set or equivalent database transaction semantics.
- Lease token and monotonically increasing checkpoint version fence stale workers.
- Heartbeat, checkpoint, completion, failure and approval transitions must validate the current lease.
- Expired lease permits reclaim only after the old worker is fenced; stale workers cannot write checkpoints or acknowledge completion.
- Use bounded exponential backoff with jitter for explicitly transient errors only. Never retry authorization failures, invalid contracts, deterministic test failures, or missing approvals as if transient.
- Cancellation interrupts in-flight work where possible and prevents subsequent writes after lease revocation.
- Outbox/inbox and idempotency keys prevent lost events and duplicate side effects; reconcile ambiguous external side effects before retry.
- Preserve immutable task events and evidence; do not rewrite history to make a failed attempt appear successful.

## 8. Alternative-solution protocol

For any non-trivial design choice, the Alternative Solver must provide:

| Dimension | Required analysis |
|---|---|
| Correctness | Does it satisfy the stated behavior and edge cases? |
| Compatibility | Existing API, schema, migration order, workers, clients and deployment |
| Security | Tenant isolation, least privilege, secret exposure, sandbox and supply chain |
| Reliability | Retry, idempotency, recovery, concurrency, cancellation and data integrity |
| Performance | Measured latency/throughput/resource impact or explicitly NOT MEASURED |
| Operations | Observability, backup/restore, rollback, support burden |
| Cost | Model, compute, storage, network and maintenance cost |
| Evidence | Reproducible test, benchmark, authoritative source or clearly marked assumption |

Decision rules:
- Compare the current approach plus at least one alternative; require two alternatives when the change is architectural or high risk.
- Mark each claim as VERIFIED, INFERRED, ASSUMPTION, or UNKNOWN.
- Select the least-complex option that meets acceptance criteria and constraints.
- If evidence is insufficient or trade-offs cross an owner boundary, return NEEDS_DECISION.
- Record rejected options and why; do not silently discard them.

## 9. Quality and evidence gates

A chunk is accepted only when all applicable gates pass:

- Contract/schema validation and DAG validation.
- Scope/path allowlist and protected-file checks.
- Formatting, lint, type/static checks appropriate to the language.
- Unit tests and relevant integration/contract tests.
- Regression test reproducing the original defect.
- Tenant isolation and authorization tests for multi-tenant paths.
- Security scan and dependency/container scan where relevant.
- Migration forward-compatibility, lock/runtime impact, backup and rollback review where schema changes occur.
- Build artifact provenance and digest binding where artifacts are produced.
- Independent QA review by a different identity from the implementer.
- Current CI status for the exact PR head SHA, not an earlier commit.
- Human approval for policy-protected actions.

Evidence manifest minimum:
`chunk_id`, `task_id`, `tenant_id`, `repository`, `base_sha`, `candidate_sha`, `changed_paths`, `tool_profile`, `command_or_suite_id`, `started_at`, `finished_at`, `exit_status`, `result`, `report_uri`, `report_sha256`, `verifier_identity`.

Never claim a test passed when it was skipped, unavailable, mocked, or run against a different SHA. Label such checks NOT RUN, SKIPPED, MOCKED, or NOT VERIFIED.

## 10. Standard engineering chunk sequence

| Chunk | Name | Deliverable | Exit gate |
|---|---|---|---|
| C00 | Baseline & evidence inventory | Current SHA, architecture map, open PR/branch map, test/CI baseline, risks | Facts separated from assumptions |
| C01 | Requirements & contracts | Problem statement, invariants, API/schema/ADR delta | Contract review accepted |
| C02 | Architecture & alternatives | Options matrix, selected design, dependency DAG, threat model | Decision owner accepts material choices |
| C03 | Implementation slice | Small scoped patch, changelog and focused tests | Static checks and focused tests pass |
| C04 | Repair & regression | Reproduction, root cause, minimal fix, regression test | Regression fails before/fixes after |
| C05 | Independent QA & security | Exact-SHA test and security evidence | Independent verifier passes |
| C06 | Integration & migration | Topological integration, compatibility and migration review | No unresolved conflict or migration collision |
| C07 | Staging qualification | Staging smoke, load/soak, observability, recovery evidence | Environment-specific gates pass |
| C08 | Release decision | Release manifest, approvals, rollback and operator sign-off | Explicit human authorization |
| C09 | Post-release learning | Incident/quality findings, metrics, follow-up chunks | Evidence retained; new work linked |

Chunks may be split or repeated; do not skip a gate merely because a later chunk exists.

## 11. RusaAi-specific integration map and constraints

The repository already has substantial in-progress or proposed work for agent runtime, DAG execution, worker lease fencing, task memory, Agentic DevOps contracts, code refinery, RAG governance and platform control-plane foundations. In particular, the open Agent Runtime PR #116 describes staged registry/capability routing/DAG execution and must be reviewed against the current mainline before overlapping implementation is proposed.

Therefore:
- Do not create a second queue, second agent registry, second workflow event store, or parallel lease system.
- Do not assume PR #116 or any other open PR is merged or active on main.
- Before coding, compare the exact current main SHA with each candidate PR head and inspect changed paths, migration numbers, CI and dependency stack.
- Treat PR descriptions and historical CI as claims to verify against the exact current head.
- Keep RusaModels as the separate Model Plane; RusaAi remains the Control Plane and worker requests must use the approved gateway boundary.
- Keep RAG_Cerdas / IM19 isolated from the RusaAi canonical knowledge store unless a superseding, approved cross-repository ADR explicitly changes that boundary.
- Do not activate proposed agent roles merely because they appear in documentation or a registry file.

## 12. First execution backlog for the agent team

1. **C00 — Mainline inventory:** inspect current main tree, branch/PR graph, migrations, workflows, tests, security findings and exact CI state.
2. **C01 — PR #116 reconciliation:** compare its Agent Runtime implementation with main and related PRs; identify overlaps, missing adapters and dependency order.
3. **C02 — Worker composition readiness:** verify which registered roles have real host-injected handlers, which are configure-only, and which are documentation-only.
4. **C03 — Independent verification boundary:** verify verifier identity separation, exact-SHA evidence binding, test skip semantics and approval non-bypass.
5. **C04 — Recovery and idempotency:** verify lease expiry, stale-worker fencing, crash/reclaim, cancellation races and external side-effect reconciliation.
6. **C05 — Capability and tenant security:** verify tool arguments, paths, tenant identity, repository scope and worker identity are host-derived and enforced.
7. **C06 — CI and vulnerability disposition:** inspect exact-head CI and raw scanner findings; distinguish fixable from unfixed findings and record owner decisions.
8. **C07 — Controlled integration plan:** propose a non-destructive merge order and rollback/forward-fix plan; no merge or production activation in this documentation increment.

Every backlog item must be converted into a chunk contract with exact source evidence before implementation starts.

## 13. Definition of Done

A project-level goal is DONE only when:
- every required chunk is PASSED or explicitly waived by an authorized human with rationale;
- no unresolved BLOCKED, FAILED, NEEDS_REPAIR or NEEDS_DECISION item is hidden;
- all results bind to the final commit/artifact digest;
- exact-head CI and independent QA are green;
- security, migration, operational and rollback gates are satisfied;
- required human approvals are recorded and valid;
- deployment/staging claims are supported by live environment evidence.

Otherwise report the precise state: PARTIAL, BLOCKED, NOT VERIFIED, or NO-GO.

## 14. Change control

This document is a proposed operating standard. Runtime/schema changes require separate scoped implementation chunks, tests, review, and an approved PR. Do not merge this document or activate any role solely because this plan exists.
