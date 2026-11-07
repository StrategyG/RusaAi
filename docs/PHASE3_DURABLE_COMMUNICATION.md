# Phase 3A — Durable DevOps communication foundation

## Scope

Phase 3 is deliberately split into independently reviewable increments. This PR implements the PostgreSQL canonical workflow intake and transactional messaging foundation only. It does not start RabbitMQ, Kafka, or a gRPC server and does not route existing AICode jobs through the new tables.

- **3A — canonical state + transactional outbox/inbox (this PR):** PostgreSQL schema, tenant-scoped workflow intake, idempotency, relay leases, consumer deduplication, and the internal Protobuf service contract.
- **3B — RabbitMQ command transport:** capability/priority queues, publisher confirms, manual consumer acknowledgements, per-queue DLQs, and a relay that settles outbox rows only after broker confirmation.
- **3C — Kafka event log:** lifecycle/policy/audit topics, partition key tenant_id + run_id, independent consumer groups, event replay, and version-aware projections.
- **3D — gRPC runtime:** mTLS service identity, deadline propagation, server-side tenant binding, and generated stubs from the versioned Protobuf source.
- **3E — end-to-end reliability:** crash-after-commit-before-ACK, broker outage, redelivery, stale lease, ordering gaps, tenant isolation, and recovery tests.

## Source-of-truth boundaries

- PostgreSQL devops_workflows is the canonical run intake and current workflow status store.
- PostgreSQL devops_tasks stores task identity, attempt, lease epoch, state version, deadline, task contract, and result contract.
- devops_event_outbox is the only publication handoff. State and its outgoing message are written in the same PostgreSQL transaction.
- RabbitMQ is the command transport in Phase 3B. It is not the canonical workflow state.
- Kafka is the durable event log in Phase 3C. It is not a command queue and consumers must not execute work from replayed analytics events.
- devops_event_inbox deduplicates a consumer's local database effect by tenant, consumer, and message ID.
- The existing AICode aicode_jobs queue is unchanged. No migration or worker cutover is implied.

## Delivery rules

1. Producers validate the complete versioned workflow event before persistence.
2. Tenant identity comes from authenticated workload/request context. The event tenant must match it; a payload cannot select another tenant.
3. A tenant-scoped idempotency key is bound to a canonical request SHA-256. Same key + same body returns the original run. Same key + different body is rejected.
4. A workflow and its first task.request command are committed atomically.
5. A relay claims pending rows with FOR UPDATE SKIP LOCKED, a random lease token, and an expiry. An expired relay lease can be reclaimed.
6. A broker adapter must wait for RabbitMQ publisher confirm or Kafka delivery acknowledgement before calling mark_published. A publish timeout is ambiguous and must retry with the same message ID.
7. Relay settlement is fenced by the lease token. A stale relay cannot mark a row published or schedule its retry.
8. Consumers insert their inbox deduplication record and apply their local database handler in the same transaction. A handler failure rolls back the inbox row so redelivery can retry.
9. Outbox retries use bounded attempts and can transition to dead_letter; DLQ handling must be observable and operator-controlled.

## Database security

Migration 007_devops_durable_communication.sql creates separate NOLOGIN groups for API, worker, and relay identities. Deployment must provision distinct LOGIN roles as members of exactly one matching group. The relay role is intentionally cross-tenant because it only transports opaque, already-authorized envelopes; it must not invoke tools or read workflow payload references. API and worker workflow access is constrained by forced row-level security using rusa.tenant_id.

The runtime role groups are not login credentials. Do not run the relay with a superuser, migration owner, API login, or worker login. The current migration grants only the relay group access to claim and settle outbox rows.

## Protobuf

contracts/proto/rusa/devops/v1/workflow.proto is the source contract for internal control RPCs. mTLS workload identity authenticates the caller. The explicit tenant field is checked against authenticated identity, not trusted as an authorization claim. Every state mutation carries attempt, lease epoch, and expected state version so the service can reject stale writers with compare-and-set semantics.

Generated stubs and a live gRPC server are intentionally deferred to Phase 3D. JSON workflow events remain the broker-neutral durable envelope.

## Deployment and migration

Apply migrations in numeric order after 006_aicode_idempotency.sql. Migration 007 does not alter aicode_jobs. It creates the rusa_relay_runtime NOLOGIN group; production provisioning must create a dedicated rusa_relay_login and grant membership using the secrets manager. Do not enable broker dispatch until the broker adapter and publisher-confirm integration tests in Phase 3B pass.

## Exit criteria for Phase 3A

- PostgreSQL integration suite applies migrations 001–007.
- Same idempotency key and body returns one run and one command.
- Same idempotency key with a different body is rejected.
- Outbox claim is lease-fenced; only the current lease can settle a row.
- Inbox replay invokes the local handler once.
- Tenant mismatch is rejected before persistence.
- Existing AICode queue tests and all existing CI gates remain green.

## Deferred production work

This phase does not claim exactly-once delivery. The transport is at-least-once; correctness comes from stable message IDs, idempotent consumers, inbox/outbox transactions, fencing, and reconciliation. It does not yet implement RabbitMQ/Kafka clients, gRPC runtime, WebSocket/SSE gateway, audit sink integration, broker HA, secrets provisioning, or production deployment.