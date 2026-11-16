# Agent/Worker Mining — PR #116 Reconciliation

**Snapshot:** 2026-11-14  
**Main baseline:** `1adac8d340e5870570910168b6626fe285dd2cf2`  
**Candidate:** PR #116, exact CI head `6e64225785289695c3bc6dfced09f1b80b26a56f`  
**Disposition:** CI-verified candidate; HOLD for review; activation BLOCKED.

## Executive finding

RusaAi already has a substantial proposed Agent Runtime in PR #116. Do not build a second registry, DAG executor, task queue, lease system, checkpoint store, or worker orchestrator. PR #116 is the primary implementation candidate and reuses the existing AICode queue, task memory, Tool Gateway, Policy Engine, sandbox and audit boundary.

The PR diff contains 28 files (+6,858 / -21): registry/schema, capability router, task decomposer, worker execution engine, runtime and API integration, two migrations, and expanded tests. It is still a draft and is not on main.

## Mainline boundary

Inspected mainline `rusa_agents/runtime.py`, blob SHA `35f82db0a5a859b706e80b1141bf6fff5864116d`. Main already contains the six-role AICode pipeline, PostgreSQL queue and task memory, lease heartbeat/ownership checks, sandbox/tool gateway, audit, and optional RAG service.

PR #116 adds the following candidate-only files:
- `rusa_agents/capability_registry.py`
- `rusa_agents/capability_router.py`
- `rusa_agents/task_decomposer.py`
- `rusa_agents/worker_execution.py`
- `rusa_agents/runtime_v1.py`
- `rusa_agents/data/agent_worker_registry.v1.json` and schema
- `migrations/014_aicode_memory_checkpoint_cas.sql`
- `migrations/015_aicode_task_approval.sql`

## Candidate architecture review

| Layer | Candidate design | Result |
|---|---|---|
| Registry | 10 core agents, 6 AICode workers, 8 DevOps profiles; versioned manifest/schema | Present on PR branch; disabled |
| Router | Explicit capability IDs; deterministic route preview | No inferred tool grants |
| Planner | Typed task contracts, DAG dependency/cycle validation | Separated from execution |
| Worker engine | Bounded sequential execution, policy gate, independent verifier, retries, cancellation, checkpoints | Injected adapters still a production dependency |
| Queue | Explicit `agent_runtime_v1` mode through existing queue/runtime | Reuses current durable queue |
| Recovery | Lease-bound checkpoint I/O and monotonic checkpoint CAS | PostgreSQL tests in candidate CI |
| Approval | Exact plan fingerprint, eligible pending nodes, tenant-scoped policy audit | Production human identity/SoD workflow still needs review |
| API | Authenticated intake, tenant from principal, server task ID, idempotency key, repository allowlist | Must be verified against production auth/config |

The PR preserves the legacy AICode path. This is the correct direction: Agent plans coordinate workers; Policy Engine and Tool Gateway remain authorities for permissions and execution.

## Registry and knowledge constraints

The manifest states `draft_proposed_not_activated`; all roles are disabled/not verified. Intake is rejected while execution is disabled. Worker handlers are not configured by default in the production composition root. Do not activate the registry simply to make a test or demo run.

The manifest records 34 provisional capability IDs, `source_chunks_total=0`, and 24 candidate source references. These are not newly authored, rights-cleared, reviewed, approved, embedded, indexed or published knowledge chunks. The role-to-source crosswalk remains pending review. Capability IDs must never be counted as knowledge chunks or used to grant permissions.

## CI evidence

Exact candidate head: `6e64225785289695c3bc6dfced09f1b80b26a56f`  
[GitHub Actions run 43396724783](https://github.com/StrategyG/RusaAi/actions/runs/43396724783)

All three observed jobs completed successfully:
1. Test, lint and security.
2. RAG and MCP integration.
3. Production image build/test/scan.

PR-reported results: 389 passed / 6 skipped in the Python suite; 65 passed in RAG/MCP integration. PostgreSQL Agent Runtime queue-to-DAG, checkpoint CAS and lease-fencing tests were included.

**Security qualification:** raw Trivy reports still show 103 HIGH (API), 118 HIGH (agent sandbox), 117 HIGH (worker), and 103 HIGH (RAG/MCP), 0 CRITICAL. The configured blocking gate passed with `--ignore-unfixed`. This is not a zero-vulnerability result; residual HIGH findings require remediation or documented VEX disposition.

## Existing foundation and compatibility

PR #116 modifies existing core paths: `runtime.py`, `queue.py`, `memory.py`, `autonomous.py`, `agent_tools.py`, and `llm-project/serve/app.py`. Preserve the current six-worker pipeline, role-scoped tool allowlists, task idempotency, lease ownership, evidence-bound QA, retry bounds and reconciliation of ambiguous external side effects. The worker-hardening and queue/idempotency work documented in PRs #57 and #40 are invariants to retain, not systems to replace.

## Chunk status

| Chunk | Status | Evidence / exit condition |
|---|---|---|
| C00 Baseline inventory | PARTIAL | PR #119 contains a detailed baseline package but remains draft. This mining independently verified relevant main SHA, main runtime blob, PR #116 diff and CI. Full inventory acceptance awaits review of PR #119 evidence/discrepancies. |
| C01 Reconcile existing worker foundation | PASS — scope mapping only | Candidate diff and mainline runtime inspected; no duplicate control plane should be built. Not merge approval. |
| C02 Runtime contract/design review | PARTIAL | Registry, router, DAG, executor and API exist on candidate branch. Production adapters and complete handler coverage remain open. |
| C03 Lease/checkpoint/recovery | PARTIAL | PostgreSQL CAS, lease-fencing, race and queue-to-DAG tests in CI. Handler-side idempotency and crash/reconciliation proof for every external side effect remain open. |
| C04 Knowledge crosswalk/governance | BLOCKED | Zero source chunks declared; candidate crosswalk pending rights, provenance, deduplication, review and approval. |
| C05 Security/operations | BLOCKED | Residual HIGH vulnerability debt; staging/soak, backup/restore, rollback, durable human approval and production adapters not evidenced. |
| C06 Integration/release | NOT STARTED | Requires reviewed PR, exact-head checks, resolved security/operations gates and explicit owner authorization. |

## Mandatory gates before merge or activation

1. Reconcile PR #116 against PR #119 and refresh exact base/head SHAs.
2. Review API intake, tenant scope, repository allowlists, worker composition, approval actor derivation, cancellation and exception paths.
3. Prove production Policy Engine and independent verifier adapters are real, fail-closed implementations.
4. Prove handler-side idempotency and read-after-write reconciliation across worker crash/lease replacement.
5. Complete durable human approval with authenticated identity, separation of duties and audit provenance; retry must never imply approval.
6. Review the canonical role-to-source-chunk crosswalk, rights, exact versions, deduplication and publication gates.
7. Remediate or formally disposition raw HIGH vulnerabilities.
8. Run staging tenant-isolation, adversarial sandbox, concurrency/load/soak, restart, backup/restore, alerting and rollback tests.
9. Obtain project-owner, security and operations approval before registry activation or deployment.

## Final decision

- **Engineering candidate:** PROMISING; CI-verified at recorded SHA.
- **Merge:** HOLD pending independent review and gate closure.
- **Runtime activation:** BLOCKED.
- **Production readiness:** NOT VERIFIED / NO-GO.

This report is documentation-only. It does not merge PR #116/#120, activate roles, apply migrations, grant tools, publish knowledge or deploy services.
