# Agent–Worker Runtime Implementation Audit

**Status:** PARTIAL — implementation is staged; production activation is NOT AUTHORIZED.  
**Repository:** `StrategyG/RusaAi`  
**Base branch:** `main`  
**Baseline SHA:** `1adac8d340e5870570910168b6626fe285dd2cf2`  
**Implementation branch:** `feat/agent-worker-runtime-audit-20261115`  
**Related design contract:** `docs/agents/AGENT_WORKER_ENGINEERING_CHUNK.md` on the separate documentation branch.  
**Reviewed candidate:** PR #116, exact head `faf686ac85875a930221ed92a357a9520a5947e5`, CI run [43403193633](https://github.com/StrategyG/RusaAi/actions/runs/43403193633).

## 1. Executive result

The implementation reuses the existing PostgreSQL AICode queue, lease tokens, tenant RLS, task memory, Tool Gateway, sandbox, inference gateway and audit store. It does not create a second queue, registry, DAG executor or model runtime.

The Agent Runtime v1 candidate adds a versioned registry, deterministic capability routing, task-plan/DAG validation, sequential worker execution, durable CAS checkpoints, lease-bound checkpoint I/O, explicit approval, queue dispatch and independent result-verification hooks. The registry remains `draft_proposed_not_activated`; the 24 role definitions remain disabled. New role permissions are not granted by registry membership.

This implementation branch adds three hardening changes after reviewing the candidate:
1. **Approval bypass closed:** generic retry now accepts `failed` tasks only. `awaiting_approval` tasks must pass the explicit, fingerprint-bound approval path.
2. **Backlog and concurrency bounded:** tenant pending work is capped, and worker claims are serialized around a global active-worker ceiling.
3. **Node audit evidence:** durable audit events record actor, root task, node, role, tool allowlist, permission scope, risk, decision, result status and counts. Raw model output, tool arguments and secrets are excluded.

The exact-head CI result cited above belongs to the reviewed PR #116 candidate, not this implementation branch. The implementation branch still requires its own exact-head CI evidence.

## 2. Architecture boundary

- **RusaAi = Control Plane:** task intake, identity/tenant authorization, task state, queue/lease, DAG orchestration, policy gates, Tool Gateway, audit and retrieval authorization.
- **RusaModels = Model Plane:** model artifacts, GGUF/llama.cpp runtime, embeddings, model residency and Model Server.
- Worker model calls must use the RusaAi authenticated inference gateway and its durable admission controller. No new direct worker-to-model bypass is introduced.
- RAG retrieval remains tenant- and project-scoped. The Agent Runtime registry's candidate source crosswalk is not an approval to publish or route knowledge chunks.

## 3. Baseline and evidence

| Evidence | Observed state |
|---|---|
| Default branch | `main` |
| Baseline commit | `1adac8d340e5870570910168b6626fe285dd2cf2` |
| Existing durable queue | `rusa_agents/queue.py`, PostgreSQL `aicode_jobs`, `FOR UPDATE SKIP LOCKED` |
| Lease fencing | Migration 002, per-claim UUID token, heartbeat and lease-conditional transitions |
| Tenant isolation | Migration 004 RLS and separate API/worker DB login groups |
| Task idempotency | Migration 006, unique tenant/idempotency key and canonical payload comparison |
| Policy audit | Migrations 005 and 008, append-only task policy events |
| Inference admission | Migration 010 and PostgreSQL-backed admission controller |
| Project-scoped RAG | Migrations 011 and 012; retrieval uses tenant/agent/project scope |
| Candidate Agent Runtime CI | Run 43403193633: all 3 jobs successful on exact PR #116 head `faf686ac85875a930221ed92a357a9520a5947e5` |
| Candidate test evidence | 389 passed, 6 skipped; 65 RAG/MCP integration tests passed; Ruff and Bandit passed. These results do not cover this branch's additional changes. |
| Production certification | NOT CERTIFIED |

The candidate run's image scan still reported raw HIGH findings (103 API, 118 agent sandbox, 117 worker, 103 RAG/MCP; 0 CRITICAL). The configured gate used `--ignore-unfixed`; this is not a zero-vulnerability result.

## 4. Traceability matrix

Statuses refer to source and tests on this implementation branch. A passing candidate CI run is not substituted for exact-head verification.

| Requirement | Status | Implementation / evidence | Remaining condition |
|---|---|---|---|
| Versioned task contract and DAG validation | IMPLEMENTED | `rusa_agents/task_decomposer.py`, `capability_registry.py`; `tests/test_agent_runtime_v1.py` | Contract/role owner approval before activation |
| Lifecycle state and dependency blocking | IMPLEMENTED | `worker_execution.py`; DAG, cycle, dependency and blocked-node tests | Operational state-transition review |
| Durable task queue | IMPLEMENTED | Existing PostgreSQL `aicode_jobs`; `queue.py` | Production migration rollout evidence |
| Bounded pending queue | IMPLEMENTED | `RUSA_MAX_PENDING_TASKS_PER_TENANT` (default 1000); tenant advisory transaction lock and active-state count | Capacity must be tuned from measured workload |
| Bounded active worker claims | IMPLEMENTED | Global advisory transaction lock plus `RUSA_MAX_PARALLEL_WORKERS` (default 1) | All worker replicas must use the same setting; load/soak evidence remains |
| Lease heartbeat and stale-worker fencing | IMPLEMENTED | Lease UUID, expiry checks, `with_lease_owned`, `LeaseBoundCheckpointStore` | Adversarial multi-process soak still required |
| Checkpoint CAS and resume | IMPLEMENTED | Migration 014, `PostgresMemory.compare_and_set`, plan fingerprint | Apply migration 014 after migration 013 dependency |
| Tenant idempotency | IMPLEMENTED | Migration 006, tenant/key uniqueness and payload comparison | External side-effect idempotency remains PARTIAL |
| Bounded retries | IMPLEMENTED | Queue max attempts restricted to 1–10; node retry limit validated | Handler-specific external side-effect idempotency still required |
| Approval separation | IMPLEMENTED | Explicit `approve()`, plan fingerprint and eligible node IDs; retry now only accepts `failed` | Production human identity/role provisioning and operator workflow remain |
| Deadline and cancellation propagation | IMPLEMENTED | Per-node deadlines, cancellation event, heartbeat lease-loss propagation | Cancellation cannot prove termination of every external side effect; resource cleanup soak remains |
| Tenant/project isolation | IMPLEMENTED | API-derived tenant, repository allowlist, worker handler tenant checks, RLS, project-scoped retrieval | Production adversarial isolation review remains |
| Tool allowlists and sandbox | PARTIAL | Existing role tool allowlists, scoped adapter, sandbox and protected paths | Production policy adapter and adversarial sandbox assessment remain |
| Audit actor/task/tool/scope/time/decision/result | PARTIAL | HMAC-chained audit store; node result event records actor/task/node/tool allowlist/scope/time/decision/status without raw output | Per-tool invocation audit is not yet emitted at every Tool Gateway call |
| Independent verification | PARTIAL | Injected verifier contract, mandatory tester/security DAG checks and CI verification path | Production verifier adapter and independent QA ownership remain |
| Inference gateway / Model Plane boundary | IMPLEMENTED | Worker gateway provider, tenant credentials, durable inference admission; RusaModels remains Model Plane | Production TLS endpoint and deployed identity evidence remain |
| Tenant/project-scoped RAG | PARTIAL | Existing knowledge service and scoped retrieval path | Canonical source/role crosswalk review; source rights and publication lifecycle dependency PR #115 |
| Registry activation | BLOCKED | Registry remains draft and all roles disabled | Explicit project-owner approval, role/source crosswalk and reviewed adapters |
| Production readiness | NOT_VERIFIED | No deployment evidence in this audit | Load/soak, backup/restore, rollback drill, monitoring/SLO, operator sign-off |

## 5. Findings and remediation

### F-01 — Retry could bypass human approval

The reviewed candidate allowed `retry()` to transition `awaiting_approval` to `queued`. Its PostgreSQL integration test asserted that behavior. That contradicted the explicit approval boundary and allowed a generic retry permission to act as approval.

**Remediation:** `retry()` now transitions only `failed` tasks. Approval-pending work must use `approve()`, which verifies tenant, plan fingerprint and eligible node IDs. Regression test: `test_postgres_queue_retry_cannot_bypass_explicit_approval`.

### F-02 — Pending queue was unbounded

The durable queue had no tenant backlog ceiling.

**Remediation:** admission serializes per tenant with a transaction-scoped advisory lock and counts `queued`, `running` and `awaiting_approval` rows before insertion. Idempotent replay is checked before capacity, so a valid replay remains safe when the tenant is at capacity. API intake returns HTTP 429 with `Retry-After: 30`.

### F-03 — Worker claim concurrency was not globally bounded by the queue

A per-process sequential executor does not limit concurrent work across multiple worker replicas.

**Remediation:** claims serialize around a global advisory lock and check active, unexpired `running` leases against `RUSA_MAX_PARALLEL_WORKERS`. Default is 1. This setting must be identical across worker replicas.

### F-04 — Node-level audit evidence was incomplete

Task-level policy events did not show the selected node's tool/permission scope and result.

**Remediation:** `agent_runtime.node.result` events are written to the existing HMAC-chained audit store. Details contain node ID, selected/delegating role, scope, tool allowlist, requested permissions, risk class, decision, result status, attempts and evidence/artifact counts. Raw model content, tool arguments and credentials are intentionally excluded. Actual per-tool invocation audit remains open.

## 6. Migration order and deployment strategy

No migration was applied to a live database by this work.

| Order | Migration | Dependency / purpose |
|---|---|---|
| 1 | 001–012 | Existing baseline migrations |
| 2 | 013 `013_rag_knowledge_governance.sql` | Reserved by open PR #115; its governance schema is a prerequisite in the shared migration sequence |
| 3 | 014 `014_aicode_memory_checkpoint_cas.sql` | Adds monotonic checkpoint version for CAS |
| 4 | 015 `015_aicode_task_approval.sql` | Extends task policy audit action constraint with `task.approve` |

**Deployment order:** take and verify a database backup; verify migrations 001–013 and role grants; stop/drain all workers; apply 014 and 015 in order; verify the new column and action constraint; deploy API and worker images from the same reviewed source; run tenant-isolation, queue, lease, approval and gateway smoke tests in staging; keep registry activation disabled until role/source approval is complete.

**Rollback:** roll back application images to the previous compatible release first. Migration 014 is additive; leave its unused column in place during application rollback. Migration 015's expanded action constraint is backward-compatible with older code; leave it in place unless there are no `task.approve` rows and a DBA-approved rollback is explicitly required. Never drop checkpoint data or truncate audit events as part of rollback. Any schema reversal requires a separately reviewed migration and verified backup.

## 7. Exact-head verification still required

The following commands must be run by CI on this implementation branch's exact head. Do not infer results from PR #116:

```bash
python -m compileall -q rusa_agents llm-project/serve
ruff check rusa_agents tests llm-project/serve
bandit -q -r rusa_agents llm-project/serve
python -m pytest tests/test_agent_runtime_v1.py tests/test_queue_races.py tests/test_queue_approval_unit.py tests/test_postgres_backends.py tests/test_postgres_rls.py tests/test_app.py tests/test_cpu_inference_compose.py -q
python -m pytest -q
docker compose -f docker-compose.yml -f docker-compose.cpu.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml config --quiet
```

PostgreSQL tests require the CI PostgreSQL service and the repository's configured `RUSA_DATABASE_URL`. A local skip is not a pass.

## 8. Open risks and approval gates

1. PR #115's migration 013 is still open; migration sequence must not skip it.
2. Production Policy Engine and independent verifier adapters are not complete.
3. Actual Tool Gateway invocation audit is incomplete.
4. External side-effect idempotency across worker crash/restart is not proven.
5. The registry source crosswalk and role approvals are pending.
6. The candidate's residual HIGH image findings need package remediation or a documented VEX disposition.
7. No production load/soak, adversarial penetration, backup/restore, rollback drill, monitoring/SLO evidence or operator sign-off was produced.

**Production readiness: NOT CERTIFIED.** No production activation, deployment, destructive migration, secret change, permission grant or merge is authorized by this implementation.
