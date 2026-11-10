# ADR-PLATFORM-001/002 — Orchestrator hierarchy and communication evolution

**Status:** Accepted for the Platform Core 2.0 migration baseline  
**Date:** 10 November 2026  
**Owners:** Platform Engineering  
**Reviewers:** Security, SRE, Software Engineering domain owner

## Context

The source architecture uses several overlapping names for orchestration and proposes gRPC, RabbitMQ, Kafka, and WebSocket as communication technologies. The current repository does not implement those proposed services. It implements an AICode-specific orchestrator and PostgreSQL-backed task queue with tenant scoping, idempotent intake, leases, lease fencing, cancellation, and audit.

The supplied All Agents specification defines Supreme Orchestrator as the global coordinator. The Agentic DevOps Lifecycle specification defines a DevOps Orchestrator scoped to the software delivery lifecycle. The communication ADR uses Executive Orchestrator. The repository uses AICodeOrchestrator as a concrete implementation class.

## Decision 1 — Canonical orchestrator hierarchy

The canonical hierarchy is:

1. **Supreme Orchestrator** — proposed global cross-domain coordinator for Software Engineering & DevOps, Intelligence, Business, and Ecosystem. It is not currently registered or active.
2. **Domain Orchestrator** — generic role for a domain-specific workflow coordinator.
3. **RUSA DevOps Orchestrator** — Software Engineering domain coordinator for the Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor lifecycle. It is a target capability, not a current runtime registration.
4. **AICodeOrchestrator** — current Python implementation for the existing coding workflow. Preserve this class and its public behavior during migration.
5. **Executive Orchestrator** — legacy/alternate terminology in ADR-COMM-001. It is not a separate agent identity and must not be independently registered.

Supreme Orchestrator may coordinate domain runs but cannot override platform policy, domain policy, approval requirements, or Tool Gateway enforcement. AICodeOrchestrator is not the global Supreme Orchestrator.

## Decision 2 — Current communication baseline

The implemented baseline remains:

- External API: HTTPS/JSON.
- Durable AICode task intake and worker ownership: PostgreSQL aicode_jobs.
- Ownership and recovery: leases, lease tokens, heartbeats, expiry recovery, retry limits, and tenant-scoped checks.
- UI status: authenticated polling of the existing task APIs. There is no external WebSocket status stream.
- Event delivery: no general-purpose event broker is currently required by the implementation baseline.

## Decision 3 — Evolution of communication

The proposals in ADR-COMM-001 are retained as future options, not current deployment requirements:

- **PostgreSQL remains the initial workflow source of truth and durable task mechanism.** General workflow/run/task state is added incrementally without replacing aicode_jobs.
- **Transactional outbox/inbox is the first event reliability step.** State and event intent must commit atomically; consumers deduplicate by tenant and message identity.
- **RabbitMQ is deferred** until measured command throughput, queue isolation, or backpressure requirements justify its operational cost.
- **Kafka or an equivalent event log is deferred** until replay, retention, independent fan-out, or analytics workloads justify it.
- **gRPC + Protobuf is deferred** until an internal service boundary benefits from independent typed RPC and has an explicit service identity/deadline contract.
- **WebSocket/SSE is deferred.** A future stream must define tenant authorization, cursor semantics, reconnect, replay, snapshot fallback, and stale-version handling. The current Control Center continues to poll.

Do not operate RabbitMQ and Kafka by default. Broker adoption requires a separate ADR with measured requirements, migration/rollback plan, operational ownership, capacity model, and failure/recovery tests.

## Consequences

- Existing AICode API, queue, six worker roles, and lease semantics remain compatible.
- New platform workflow contracts can be introduced without claiming that future agents or transports are already live.
- A PostgreSQL outbox/inbox provides an incremental reliability boundary before adding another service.
- UI and documentation must use the capability matrix and must not label planned agents as active.
- Production side effects remain approval-gated; this ADR does not authorize autonomous merge, release, deployment, permission, secret, financial, or destructive actions.

## Acceptance and evidence

- Architecture naming and communication baseline are documented in this ADR and PLATFORM_ARCHITECTURE_2.0.md.
- Current versus target capabilities are listed in AGENT_CAPABILITY_MATRIX.md.
- Existing AICode compatibility tests and workflow migration tests remain required.
- No broker or WebSocket is introduced by this decision.
