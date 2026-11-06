# RUSA-AI AICode Agent — Enterprise Foundation

## Architecture
Hierarchical master orchestrator with analyst, architect, developer, debugger, tester, and security workers. Worker providers are injected through a protocol; no provider credentials are stored in task context.

## Autonomy and trust boundaries
Autonomy is allowlist-based, not unrestricted. Repository writes and command execution must be implemented by a separately reviewed tool gateway and isolated sandbox. Workers in this foundation are report-only and cannot execute tools. Merge, deploy, release, production writes, secret/permission changes, destructive operations, and audit disabling are protected or denied.

## Lifecycle
received -> observing -> planning -> executing -> verifying -> succeeded | awaiting_approval | blocked | failed | cancelled. Only the orchestrator changes task state. Worker failures, timeouts, policy denials, and missing evidence stop completion.

## Memory and audit
SQLite memory is tenant-scoped for single-instance use. Multi-replica deployment requires a shared transactional database. Audit records are HMAC chained and redacted; the integrity key must be managed outside the repository and backed up securely.

## Evidence gate
A task cannot succeed merely because a worker returns a non-empty report. An injected verifier must independently verify test and security evidence for the same full commit SHA. The default verifier rejects all evidence.

## Production gaps
Durable queue, sandbox isolation, real repository adapter, tool authorization, distributed rate limiting, shared database, observability, backup/restore, load testing, adversarial testing, model benchmark, and operational runbooks remain required. This package is a guarded foundation, not a certified production coding agent.
