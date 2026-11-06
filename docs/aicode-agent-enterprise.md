# RUSA-AI AICode Agent — Enterprise Design

## Scope
Hierarchical coding-agent subsystem: master orchestrator plus analyst, architect, developer, debugger, tester, and security workers. The host injects a model provider and concrete repository/tool adapters; no model vendor is hard-coded.

Full autonomy applies only to allowlisted work in an isolated feature branch and sandbox. Merge, deployment, release, secret/permission changes, production writes, destructive actions, and disabling audit controls remain protected or prohibited.

## Components
- contracts.py: typed task, repository, worker, result, and state-machine contracts.
- orchestrator.py: deterministic delegation, persistence, audit, and evidence gates.
- workers.py: provider-injected role adapter.
- policy.py: allowlist, prohibited operations, and approval policy.
- memory.py: SQLite task memory using parameterized SQL.
- audit.py: append-only JSONL event log.

## Worker communication
The master passes a typed CodingTask and accumulated WorkerResult summaries. Results include success, artifacts, evidence, and blockers. A production queue can transport the same contract; consumers should be idempotent by task_id. Do not place secrets in task context.

## Lifecycle
received -> observing -> planning -> executing -> verifying -> succeeded | awaiting_approval | blocked | failed.
Only the master changes task state. A failed worker or blocker stops downstream work. Success requires test evidence and security evidence.

## Tool gateway
Implement bounded adapters for repository read, branch creation, sandbox writes, allowlisted test execution, commit, and PR creation. Never expose arbitrary shell execution to model output. Use argument arrays, timeouts, resource limits, isolated workspaces, redacted logs, and feature-branch-only writes. Protect main.

## Memory and tenancy
SQLite is a single-instance starter. Multi-tenant production must use a managed transactional database and include tenant identity in every key, query, cache key, and authorization check.

## Production readiness gates
Add durable queue/retries/idempotency, sandbox isolation, tenant-isolation tests, OpenTelemetry, metrics/alerts, tamper-evident audit retention, backup/restore, load testing, adversarial security tests, CI evidence verification, kill switch, and operator runbooks before production certification.

## Acceptance criteria
1. State machine rejects invalid transitions.
2. Workers are typed and provider-agnostic.
3. Policy rejects protected and prohibited operations.
4. Memory survives restart.
5. Audit events are attributable and append-only.
6. Worker failure stops downstream execution.
7. No task succeeds without test and security evidence.
8. Git writes are restricted to task branches; merge/deploy remain approval-gated.
9. Tenant isolation, durable queue, sandbox, CI, load, and security tests pass before production certification.
