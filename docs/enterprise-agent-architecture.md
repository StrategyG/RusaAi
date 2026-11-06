# RUSA-AI Enterprise Agent Architecture

## Architecture decision
Use a hierarchical supervisor with bounded specialist workers and controlled autonomy. The supervisor owns planning and state transitions; workers return typed results; the tool gateway is the only execution path. Full autonomy means allowlisted low-risk work can complete without per-step human input. It never means unrestricted shell, secrets, production writes, merge, deployment, release, or policy changes.

## Logical topology
Client/Admin UI -> API Gateway + tenant API-key resolver -> Agent Control Plane -> Durable Task Store -> Worker Pool -> Supervisor -> specialist workers -> Tool Registry/MCP adapters -> GitHub, RAG retrieval, sandbox runner. Audit and policy apply across all layers.

## Agent roles and communication
Supervisor decomposes goals, creates bounded JSON plans, validates tool allowlists, persists cursor, and handles retries/approval pauses. Analyst inspects repository; Architect proposes changes; Developer prepares branch-scoped changes; Reviewer inspects diffs; Tester verifies CI; Security checks scope, secrets, injection, and supply chain.
Worker messages use protocol version, task/correlation IDs, sender, recipient, type, timestamp, and JSON payload. Only the supervisor changes task state.

## Worker orchestration
Tasks are tenant-scoped and idempotent by tenant plus Idempotency-Key. Workers claim queued work using database row locks (SKIP LOCKED on PostgreSQL), bounded concurrency, lease expiry, retry with backoff, cancellation flags, and persisted step cursors. Run worker separately with python -m rusa_agents.worker_cli. PostgreSQL is required for multi-replica deployments; SQLite is local single-instance only. Apply migrations before production; create_all is bootstrap, not a migration strategy.

## Memory
Working memory: plan, cursor, tool results, blockers, approvals. Episodic memory: completed task state and evidence. Semantic memory: tenant-scoped memory table; vector RAG adapter is future work, not yet implemented. Audit memory: hash-chained events; export to immutable WORM storage and verify chain integrity operationally. Every read/write includes tenant identity; tenant keys map server-side, clients cannot select tenant via a header.

## Tools and safety
Only registered tools execute. Each task has explicit tool allowlist. GitHub writes are limited to feature/fix/chore/docs branches. No arbitrary shell or filesystem tool is registered. Tool outputs and repository contents are untrusted data. Policy is enforced outside model prompts.

## Evaluation
CI should measure plan validity, task success, tool-call accuracy, policy violation rate, CI evidence correctness, latency p50/p95, cost/request, and prompt-injection resistance using a versioned benchmark. No quality score without dataset, version, sample size, and evaluator definition.

## Production gates
Apply Alembic migrations and validate PostgreSQL concurrency and tenant isolation. Deploy separate API and worker services with autoscaling, queue-depth alerts, dead-letter/recovery, and kill switch. Add TLS gateway, rate limiting, secret rotation, GitHub App credentials, sandboxed test execution (gVisor/Firecracker), RAG provenance/ACL/deletion and retrieval benchmarks, immutable audit export, backup/restore drills, load/soak/adversarial tests, and incident runbooks. Human approval remains required for merge, release, deployment, secrets, permissions, and destructive operations.
