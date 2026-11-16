# RUSA AI Four Foundations — Merge and Migration Plan

**Snapshot:** 2026-11-09  
**Base:** `StrategyG/RusaAi:main` at `5c58e1a4f40c08ed8752c2b0f1eb79dcc422cc9f`  
**Status:** integration proposal; no PR was merged and no deployment was performed by this plan.

## 1. Open PR dependency graph

### DevOps lifecycle stack

```text
main
  └─ PR #54  Phase 1: versioned lifecycle contracts
       └─ PR #65  Phase 2: safe in-process dry-run
            └─ PR #66  Phase 3A: PostgreSQL workflow/outbox/inbox
                 └─ PR #67  Phase 3B: RabbitMQ/Kafka/gRPC transport
                      └─ PR #68  Phase 4A: specialist execution authority
                           └─ PR #69  Phase 4B: Plan + Code specialists

PR #70 is a parallel Phase 4A authorization/runtime branch based on PR #67.
It is not in PR #69's dependency chain.
```

PR #54–#69 are deliberately stacked. A child PR must be evaluated against its stated parent branch; its CI result does not independently prove that the complete stack is safe to merge to main.

### Mainline PRs

| PR | Scope | Snapshot status | Integration note |
|---|---|---|---|
| [#86](https://github.com/StrategyG/RusaAi/pull/86) | Explicit human approve/reject API, append-only approval audit, 4-core resource bounds | Open, non-draft; CI success; mergeable | P0 safety change; migration 009 conflicts with other open branches |
| [#85](https://github.com/StrategyG/RusaAi/pull/85) | Fourteen-module Control Center and source-backed role/lifecycle presentation | Open, non-draft; CI success; mergeable | Overlaps #86 in API, queue, compose, tests, and web page; rebase and reconcile |
| [#82](https://github.com/StrategyG/RusaAi/pull/82) | Agent/project-scoped RAG knowledge packs and version history | Open, draft; CI success on its head; non-mergeable | Branch is 9 commits behind main and 137 commits ahead; rebase and resolve migration/code overlap |

### DevOps PR snapshot

| PR | Scope | Snapshot status | Dependency |
|---|---|---|---|
| [#54](https://github.com/StrategyG/RusaAi/pull/54) | Versioned task/result/event contracts | Open, draft; CI success; 60 commits ahead and 20 behind main | main |
| [#65](https://github.com/StrategyG/RusaAi/pull/65) | Safe in-process dry-run with synthetic fixtures | Open, draft; CI success | #54 |
| [#66](https://github.com/StrategyG/RusaAi/pull/66) | Canonical PostgreSQL workflow state, transactional outbox/inbox, Protobuf contract | Open, draft; CI success | #65 |
| [#67](https://github.com/StrategyG/RusaAi/pull/67) | RabbitMQ command transport, Kafka event log, gRPC control plane | Open, draft; CI success | #66 |
| [#68](https://github.com/StrategyG/RusaAi/pull/68) | Specialist authority registry and gRPC task-claim enforcement | Open, draft; CI success | #67 |
| [#69](https://github.com/StrategyG/RusaAi/pull/69) | Claim-bound Plan and Code specialists | Open, draft; CI success | #68 |
| [#70](https://github.com/StrategyG/RusaAi/pull/70) | Alternative specialist authorization/runtime implementation | Open, draft; CI success | #67 |

All CI statements above refer to the observed workflow run on the listed PR head at this snapshot. They do not mean that the branch is current with main, merged, deployed, or production-certified.

## 2. Confirmed file overlap

### PR #85 and PR #86

Both change:

- `llm-project/serve/app.py`
- `rusa_agents/queue.py`
- `tests/test_app.py`
- `tests/test_postgres_backends.py`
- `docker-compose.production.yml`
- `web/app/page.tsx`

**Required action:** merge/rebase sequentially, not as independent patches. Recommended order is #86 first because it closes the approval bypass. Then rebase #85 and adapt its new Control Center to the explicit approve/reject API. PR #85's current description correctly says that its UI does not yet simulate approval actions because the backend endpoint was absent when it was authored.

### PR #82 overlap

PR #82 also changes `rusa_agents/runtime.py`, `rusa_agents/tool_gateway.py`, `pyproject.toml`, RAG services, and migration/test contracts. PR #86 changes `runtime.py` and `tool_gateway.py`; PR #82 therefore requires a reviewed rebase after the approval changes. Do not resolve this by blindly taking one branch's whole file.

### PR #68 and PR #70

Both are labelled Phase 4A specialist authorization foundations and both change the DevOps integration documentation and `rusa_agents/devops/__init__.py`. Their implementation shapes differ:

- #68 adds `agent_registry.py` and enforces registry policy in the gRPC/control-store claim path. PR #69 is built on this design.
- #70 adds a separate `agent_runtime.py` task authorization/handler boundary.

**Required action:** choose one canonical authorization path. Review and selectively port unique safeguards from the other branch. Do not merge both as independent Phase 4A implementations without a single registry, one task authorization decision, and tests proving no divergent enforcement paths.

## 3. Migration collision register

### Current main

Main currently owns versions 001–008, ending with:

- `008_aicode_task_list_audit.sql`

### Open branch collisions

| Branch | Migration filename | Collision |
|---|---|---|
| #66 | `008_devops_durable_communication.sql` | Main already owns version 008 |
| #67 | `009_devops_task_leases_and_cancellation.sql` | #86 and #82 also introduce version 009 |
| #86 | `009_aicode_explicit_approval.sql` | #67 and #82 also introduce version 009 |
| #82 | `008_rag_agent_scope_indexes.sql` | Main already owns version 008 |
| #82 | `009_rag_project_scope_index.sql` | #67 and #86 use version 009 |
| #82 | `010_rag_knowledge_version_history.sql` | Depends on a globally unique migration sequence |

The existing migration contract test requires a unique, monotonic sequence. CI on isolated branches cannot detect every collision that appears only when independently passing branches are combined.

### Proposed global sequence

If the recommended order (#86 → #85 → #54 → #65 → #66 → #67 → #68/#70 decision → #69 → #82) is accepted, renumber before merge:

| Version | Proposed filename | Origin |
|---:|---|---|
| 001–008 | Existing main migrations, unchanged | main |
| 009 | `009_aicode_explicit_approval.sql` | #86 |
| 010 | `010_devops_durable_communication.sql` | #66 |
| 011 | `011_devops_task_leases_and_cancellation.sql` | #67 |
| 012 | `012_rag_agent_scope_indexes.sql` | #82 |
| 013 | `013_rag_project_scope_index.sql` | #82 |
| 014 | `014_rag_knowledge_version_history.sql` | #82 |

This is a **proposed** sequence, not an already-applied database history. Update all migration runners, tests, deployment instructions, and rollback documentation consistently. Do not rename a migration that has already been applied in a shared environment; if any of these versions have been applied outside the repository, stop and design a forward-only repair migration.

## 4. Recommended integration sequence

### Step 0 — Freeze and record the baseline

- Record main SHA `5c58e1a4f40c08ed8752c2b0f1eb79dcc422cc9f`.
- Do not push integration changes directly to main.
- Keep the existing AICode `aicode_jobs` queue and six-worker runtime intact.
- Treat every unmerged PR as unmerged, regardless of its green CI.

### Step 1 — Merge the approval safety boundary

- Rebase #86 only if main moves.
- Confirm migration 009 is still unused in the target database history.
- Require the exact PR-head CI checks and review.
- Preserve separate approve/reject decisions; retry remains limited to failed tasks.
- Verify the 4-core resource caps against the deployment host before changing them.

### Step 2 — Reconcile the Control Center

- Rebase #85 onto the #86 merge commit.
- Integrate approval/reject actions into the new UI only through the explicit backend endpoints and role checks.
- Preserve tenant session, same-origin, and role boundaries.
- Re-run Web CI and backend CI on the rebased head.

### Step 3 — Rebase and merge DevOps Phase 1–3

- Rebase #54 onto the updated main; its observed branch was 20 commits behind main.
- Merge #54 only as contract/schema foundation.
- Merge #65 as a synthetic, zero-side-effect dry-run only.
- Renumber #66/#67 migrations to the global sequence above.
- Merge #66 before #67. Do not enable broker dispatch until publisher-confirm, crash/replay, deduplication, and reconciliation tests pass.
- Keep PostgreSQL as canonical workflow state; outbox is the publication handoff; RabbitMQ carries commands; Kafka is the event log. Delivery is at-least-once, not exactly-once.

### Step 4 — Resolve Phase 4A before Plan/Code

- Compare #68 and #70 at the implementation and policy-decision level.
- Select one canonical registry/runtime path.
- Preserve authenticated principal binding, tenant/resource checks, approval, budget, deadline, cancellation, lease epoch, and actual tool/action authorization.
- Rebase #69 onto the selected Phase 4A head and rerun the full CI stack.

### Step 5 — Integrate the RAG knowledge scope branch

- Rebase #82 onto the updated main after the #86 runtime/tool-gateway changes.
- Resolve `runtime.py` and `tool_gateway.py` manually; preserve trusted tenant, agent, and project scope derivation.
- Renumber its migrations to 012–014 if the proposed order is adopted.
- Keep original owner-supplied source documents out of the public repository.
- Keep gold-set labels pending human review; do not report retrieval metrics until the evaluation set is reviewed and run.

### Step 6 — Complete the remaining source scope

- Continue DevOps phases 4C–4E: Build/SBOM, independent Test/QA, Release, Deploy to staging, Operate, Monitor, and controlled feedback.
- Define one Business pilot with a named process owner, system of record, input/output contracts, KPIs, approval matrix, and compensation/reconciliation.
- Define Ecosystem private-beta trust controls: publisher identity, signed package, SBOM, sandbox, permission manifest, tenant consent, certification, quotas, revocation, and support.
- Do not create Business/Ecosystem orchestrators until their workflows and ownership are approved.

## 5. Source acceptance gates

The ADR's numerical criteria are **initial proposed targets**, not measured service levels:

| Test | Initial target / source criterion |
|---|---|
| Run admission | HTTP 202 p95 ≤ 500 ms after durable run and outbox commit |
| Dispatch to claim | p95 ≤ 2 s at the agreed design load |
| Cancellation under full queue | P0 cancellation observed by connected worker p95 ≤ 2 s |
| Duplicate delivery | External test effect recorded once under redelivery |
| Tenant/security | Reject cross-tenant identity, wrong audience, bad signature, expired replay, stale lease |
| QA/release | QA must validate the exact source commit and artifact digest being released |
| Side-effect uncertainty | Reconcile unknown outcomes; never blindly retry a non-idempotent operation |

Additional exit gates:

- All migration versions are unique and monotonic across the combined branch stack.
- Outbox state and workflow transition commit atomically.
- Inbox/consumer idempotency and lease fencing survive crash/redelivery tests.
- Cancellation and kill-switch block new actions and reconcile in-flight side effects.
- Monitor and Security Guard are read-only.
- Human-required actions are approved by a verified human principal.
- RAG evaluation uses human-reviewed relevant source URIs and tenant/agent/project scope.
- Business and Ecosystem have explicit owners and operational runbooks.
- Staging rollback, backup/restore, security, load, and soak evidence exist before production release.

## 6. Release decision

**Do not merge the full stack as one unreviewed batch and do not deploy it to production based only on green branch CI.**

The safe route is a sequence of small, rebased, reviewable PRs with globally unique migrations and CI run on each exact combined head. This plan itself does not merge PRs or change deployment state.
