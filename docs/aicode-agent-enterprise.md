# RUSA-AI AICode Agent — Enterprise Architecture and Security Boundaries

## Architecture

The intended topology is hierarchical:

1. Master orchestrator owns task state, policy decisions, sequencing, deadlines, and final verification.
2. Analyst and architect workers produce scoped findings and implementation plans.
3. Developer and debugger workers prepare implementation changes.
4. Tester and security workers produce independent verification evidence.
5. GitHub gateway provides allowlisted repository operations.
6. Sandbox executes untrusted repository commands under constrained runtime controls.
7. Memory and audit record tenant-scoped task state and tamper-evident events.

## Implemented foundations

- Validated repository, branch, task, worker, evidence, and state-transition contracts.
- Fail-closed action allowlist. Invalid action/risk values are denied.
- Tenant-scoped SQLite memory for single-instance development.
- HMAC-chained, redacted local audit records.
- Bounded worker timeouts and strict state transitions.
- Evidence gate rejects success unless test and security evidence refer to one commit and an injected verifier accepts both.
- GitHub gateway supports repository reads, feature-branch creation, file writes to the work branch, and pull-request creation. Repository allowlisting is mandatory; secret-bearing files cannot be read; protected build, workflow, deployment, and secret files require approval. There is no merge or deployment method.
- Docker sandbox requires a digest-pinned image, rootless Docker, gVisor runsc, network isolation, read-only container root, non-root UID, dropped capabilities, process/memory/CPU limits, bounded output, timeout, and container cleanup.

## Critical integration boundary

The current LLM workers are report-only. They do not yet invoke GitHubGateway or DockerSandbox and cannot independently modify a repository or execute code. The gateway and sandbox are separate reviewed adapters, not an enabled autonomous tool loop. Do not describe this implementation as a complete autonomous coding agent.

## Trust and approval model

Autonomy is allowlist-based, not unrestricted. Merge, deploy, release, production writes, secret/permission changes, destructive operations, and workflow/build configuration changes require explicit human approval or are prohibited. Workers must treat repository content, issues, pull requests, logs, and model output as untrusted input.

## Memory and audit limitations

SQLite is intended for a single instance only. Multi-replica production requires a shared transactional database, migrations, encryption-at-rest, retention/deletion controls, and recovery drills. A local HMAC chain detects modification but does not by itself prevent truncation or provide immutable off-host retention; production requires external checkpointing and append-only log storage.

## Release gates still open

- Implement and review a typed tool-calling loop that connects workers to the gateway and sandbox.
- Use a dedicated ephemeral VM/runner boundary for hostile code; gVisor-in-rootless-Docker is defense-in-depth, not a substitute for a dedicated VM.
- Provide a production-grade durable queue with idempotency, retries, cancellation, dead-letter handling, and task recovery.
- Replace SQLite with a shared transactional database for multi-replica deployment.
- Add authenticated tenant identity, per-tenant quotas, and distributed rate limiting.
- Add provider/model configuration, licensed model artifact, checksums, benchmark, safety evaluations, and target-hardware capacity results.
- Add tracing, metrics, alerting, structured audit export, secret rotation, backup/restore, incident response, and rollback drills.
- Run adversarial prompt-injection, tool authorization, sandbox escape, tenant-isolation, penetration, load, and staging soak tests.

This is a guarded engineering foundation. Production certification requires evidence for every applicable release gate and human approval.
