# Agent/Worker Runtime — Audit and Traceability

- **Audit date:** 2026-11-15
- **Repository:** `StrategyG/RusaAi`
- **Default branch:** `main`
- **Main baseline SHA:** `1adac8d340e5870570910168b6626fe285dd2cf2`
- **Hardening branch:** `feat/agent-worker-global-admission-20261115`
- **Design contract reference:** `docs/agents/AGENT_WORKER_ENGINEERING_CHUNK.md`,
  commit `45724002472378c5c976a0fc7c094064a8b0dbeb`
- **Primary runtime candidate:** [PR #116](https://github.com/StrategyG/RusaAi/pull/116),
  currently draft and not merged.

## 1. Baseline findings

Mainline already has one durable PostgreSQL AICode queue, a six-role
specialist pipeline, worker heartbeat/lease checks, lease-token-fenced queue
transitions, repository publication fencing, tenant-aware task APIs, a
sandboxed Tool Gateway, task-policy audit, HMAC-chained audit events, optional
tenant/project-scoped RAG, and a worker inference provider that can use the
RusaAi gateway.

Mainline did **not** contain the general versioned Agent/Worker registry and
DAG executor described by the design contract. The primary implementation
candidate is PR #116. It adds the registry/router/decomposer/executor/API
integration and lease-bound checkpoint CAS, but remains draft/disabled.
Do not create a second registry, DAG executor, queue, lease system, checkpoint
store, or worker orchestrator.

The audit also found a configuration drift: the CPU operations runbook
documented one specialist worker at a time, while the runtime composition
defaulted `RUSA_MAX_PARALLEL_WORKERS` to four. This branch changes the runtime
default to one and declares the value in `.env.example`.

## 2. Traceability matrix

Status is assessed against the default branch unless the candidate column is
explicitly named.

| Requirement | Mainline status | PR #116 candidate | This branch | Evidence / remaining gap |
|---|---|---|---|---|
| Versioned task contract and general DAG state machine | PARTIAL | IMPLEMENTED — staged | NOT IMPLEMENTED HERE | Mainline has the legacy AICode task contract/state flow. PR #116 adds versioned DAG contracts and validation; it is still draft and disabled. |
| Durable task queue | IMPLEMENTED | Reuses mainline | PRESERVED | Existing `PostgresTaskQueue` remains the single source of truth. |
| Per-tenant pending queue bound | MISSING | Not the focus | IMPLEMENTED — pending CI | Tenant advisory lock + pending-state count; default 100; HTTP 429 on saturation. |
| Global active worker bound | PARTIAL | Reuses mainline queue | IMPLEMENTED — pending CI | Global advisory lock + live lease count; default one active task across worker replicas. |
| Lease expiry and stale completion fencing | IMPLEMENTED | Extends checkpoint fencing | PRESERVED | Existing lease token, worker identity, tenant, status, and expiry predicates remain. |
| Lease-bound checkpoint I/O and CAS | MISSING on main | IMPLEMENTED — candidate | NOT IMPLEMENTED HERE | PR #116 adds `with_lease_owned`, `LeaseBoundCheckpointStore`, and migration 014 CAS. |
| Intake idempotency | IMPLEMENTED | Extends DAG intake | PRESERVED | Existing per-tenant idempotency key and normalized-payload comparison remain. |
| External side-effect idempotency | PARTIAL | PARTIAL | NOT IMPLEMENTED HERE | Downstream idempotency keys and read-after-write reconciliation across crash/retry remain required. |
| Bounded retry attempts and delay | PARTIAL | IMPLEMENTED for DAG nodes | IMPLEMENTED — pending CI | Mainline now limits attempts to 1–10 and delay to 0–3,600 seconds. Explicit retries remain audited new cycles. |
| Deadline and cancellation | PARTIAL | IMPLEMENTED — staged | NOT IMPLEMENTED HERE | Mainline task timeout and heartbeat-driven cancellation exist. Cleanup and every external handler's cancellation behavior still need independent proof. |
| Sandbox and role tool allowlists | IMPLEMENTED for six AICode roles | Adds per-node narrowing | PRESERVED | Tool Gateway remains authority; no role activation or permission expansion. |
| Tenant and project scope | IMPLEMENTED for existing task/RAG paths | Adds DAG tenant binding | PRESERVED | API derives tenant from principal; RAG project scope derives from authorized repository. Adversarial deployment tests remain required. |
| Audit trail | PARTIAL | Extends approval/task events | PRESERVED | Existing append-only task-policy events and HMAC audit chain exist; normalized actor/tool/scope/result coverage is not complete for every action. |
| Worker inference gateway | IMPLEMENTED when configured | Preserves gateway boundary | PRESERVED | Worker uses tenant-scoped gateway credentials and deadline behavior; production deployment configuration still needs verification. |
| Tenant/project-scoped RAG retrieval | IMPLEMENTED — optional | Reuses existing service | PRESERVED | RAG remains opt-in and depends on deployed/verified knowledge and embedding services. |
| Independent verification adapters | PARTIAL | Injected adapter; production review pending | NOT IMPLEMENTED HERE | Existing Tester/Security evidence gates exist; production verifier adapter and calibration remain release gates. |
| Production readiness | NOT CERTIFIED | NOT CERTIFIED | NOT CERTIFIED | No staging load/soak, backup/restore, rollback drill, full monitoring evidence, or operator sign-off is established by this branch. |

## 3. Primary candidate PR #116 evidence

PR #116 is the existing primary candidate, not a second implementation to
replace. It changes 28 files and adds the versioned registry, capability
router, DAG decomposer, worker execution engine, queue/API integration,
checkpoint CAS, approval persistence, and associated tests.

The current PR metadata points to head
`faf686ac85875a930221ed92a357a9520a5947e5`. The latest observed Actions run
for that head is
[43403193633](https://github.com/StrategyG/RusaAi/actions/runs/43403193633).
All three jobs were observed completed successfully:

1. Test, lint, and security.
2. RAG and MCP integration.
3. Build, test, and scan production images.

This is candidate-branch CI evidence only. The PR body and reconciliation
report record residual raw Trivy HIGH findings (103 API, 118 agent sandbox,
117 worker, 103 RAG/MCP; zero CRITICAL) while the blocking gate ignores
unfixed findings. A successful blocking gate is not a zero-vulnerability
result. Remediation or an approved VEX disposition remains required.

## 4. This branch — bounded admission changes

This branch is deliberately additive to the existing queue; it does not
implement a second runtime.

| File | Change |
|---|---|
| `rusa_agents/queue.py` | Tenant backlog ceiling, global live-worker admission lock, attempt ceiling, retry-delay ceiling |
| `llm-project/serve/app.py` | Return HTTP 429 with Retry-After when tenant queue is full |
| `rusa_agents/runtime.py` | Default specialist parallelism aligned from four to one |
| `.env.example` | Declare the queue and worker capacity defaults |
| `tests/test_queue_capacity.py` | Unit coverage for capacity, idempotency under saturation, claim ceiling, and retry bounds |
| `tests/test_queue_races.py` | PostgreSQL concurrent enqueue and worker-claim race coverage |
| `tests/test_app.py` | API overload response regression |
| `docs/adr/ADR-0002-agent-worker-admission.md` | Decision record, constraints, rollout and rollback |
| `docs/AGENT_WORKER_RUNTIME_OPERATIONS.md` | Operations runbook |
| `docs/engineering/AGENT_WORKER_RUNTIME_TRACEABILITY.md` | This audit and matrix |

No database migration, secret, model artifact, tool grant, registry activation,
production deployment, or permission change is part of this branch.

## 5. Verification status

The code and tests are committed to the hardening branch, but this report
must be updated from the exact branch-head CI run before any PASS claim is
made. Local execution was unavailable in the audit environment because the
GitHub host could not be resolved from the container runtime. Do not treat
the PR #116 CI run as validation of this branch.

Required exact-head checks:

- `python -m compileall -q rusa_agents rusa_knowledge rusa_mcp llm-project/serve tests`
- `ruff check rusa_agents rusa_knowledge rusa_mcp tests llm-project/serve`
- `bandit -q -r rusa_agents rusa_knowledge rusa_mcp --severity-level medium --confidence-level medium`
- `python -m pytest -vv -o faulthandler_timeout=60`
- isolated sandbox test suite, image build/scan, Compose validation, and RAG/MCP integration CI.

The full PostgreSQL race tests must run against the CI PostgreSQL service.
A passing unit-only run is insufficient to prove advisory-lock concurrency.

## 6. Remaining blockers and approvals

1. Review PR #116 against the latest baseline and this queue hardening branch;
   keep the existing runtime candidate as the single Agent Runtime design.
2. Complete production Policy Engine and independent verifier adapters.
3. Prove downstream idempotency and reconciliation for Git/API/tool side
   effects across crash and lease replacement.
4. Complete authenticated human approval, separation of duties, and audit
   provenance before enabling any new role.
5. Review the canonical role-to-source-chunk crosswalk, rights, provenance,
   deduplication, and quality gates. Capability IDs are not knowledge chunks.
6. Remediate or formally disposition the residual HIGH image findings.
7. Run staging tenant-isolation/adversarial sandbox, concurrency/load/soak,
   restart/recovery, backup/restore, rollback, monitoring/alerting, and
   operator sign-off.
8. Obtain explicit project-owner, security, and operations approval before
   merge, registry activation, migrations 014/015, or deployment.

## 7. Final disposition

- **Mainline Agent/Worker runtime:** PARTIAL.
- **PR #116 runtime candidate:** PARTIAL / HOLD; draft and disabled.
- **Queue admission hardening on this branch:** implementation present;
  exact-head CI pending.
- **Runtime activation:** BLOCKED.
- **Production readiness:** NOT CERTIFIED.

CI is evidence for the checks it ran on one exact commit. It is not a
production certification.
