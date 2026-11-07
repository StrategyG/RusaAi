# Phase 3B — RabbitMQ command transport adapter

## Scope

This increment implements the broker adapter for durable commands. It does not connect the existing AICode queue or the synthetic DevOps dry-run to RabbitMQ, and it does not start a production relay or worker pool. A worker must not consume task requests until the durable claim/lease transaction and its `task.accepted` outbox event are implemented.

## Routing contract

- `task.request` routes to a direct-exchange queue derived from the validated `payload.capability` and `payload.priority`: `task.<capability>.<priority-lowercase>`.
- The topology declares one durable quorum queue for every configured capability and P1/P2/P3 class. Competing consumers share one logical command queue.
- `task.cancel.requested` always routes to the isolated `control.p0` queue. P0 is not accepted as a normal task priority in the JSON Schema.
- Lifecycle, policy, audit, progress, and completion facts are not commands and are rejected by this adapter. They belong on the Kafka event channel in Phase 3C.
- Every command queue has its own durable DLQ. Quorum delivery limits bound repeated handler failures; at-least-once dead-lettering is enabled.

## Producer guarantees

1. Validate the complete workflow event before routing.
2. Derive the route from the envelope, never from caller-supplied AMQP headers.
3. Serialize canonical UTF-8 JSON and sign the exact bytes using RFC 7797 detached JWS with Ed25519 (`alg=EdDSA`, `b64=false`, critical `b64`, and a `kid`).
4. Bind the event `producer` to the signing key's registered workload identity.
5. Publish persistent messages with stable `message_id`, `correlation_id`, mandatory routing, and publisher confirms.
6. Settle the PostgreSQL outbox row only after a positive publisher confirmation. An ambiguous timeout retries with the same message ID; consumers deduplicate.

## Consumer guarantees

- Verify the detached JWS and map its `kid` to a trusted producer identity before processing.
- Re-derive the routing key from the signed envelope and compare it with the AMQP routing key and metadata.
- Authorize the consumer identity for the event tenant through the injected tenant-authorizer callback. The tenant in an event is not an authorization claim.
- Reject malformed, unauthorized, invalid-signature, and unroutable messages without requeue so the broker dead-letters them.
- On a transient handler failure, NACK with requeue. Quorum queue delivery limits eventually move repeatedly failing messages to the per-queue DLQ.
- ACK only after the handler returns from its durable inbox/state transaction. If the ACK is lost after commit, redelivery is safe because the inbox deduplicates.

## Connection security

`connect_rabbitmq_mtls` accepts only `amqps://` URLs and requires a CA, client certificate, and private key. Hostname verification and certificate validation remain enabled, with TLS 1.2 or newer. The application does not provide a plaintext production fallback. CI unit tests use fake channels and do not establish production mTLS trust.

## Outbox relay

`RabbitMQOutboxDispatcher` claims command rows with the existing PostgreSQL relay lease, publishes each envelope, then marks the row published only after confirmation. Failures use bounded full-jitter backoff and the existing lease token. The dispatcher deliberately stores only the exception class in `last_error`, not raw broker exception text that could contain connection details.

The dispatcher signing key must map to the producer identity that created the command. In the current command contract, that is the DevOps Orchestrator workload. The relay is a transport component of that trusted workload, not a general cross-tenant agent and not an authority to invent commands.

## Current boundary and next work

This increment provides the transport primitives and unit coverage for routing, signatures, publisher confirms, manual acknowledgements, bounded requeue, topology, and outbox settlement. It does not yet provision a RabbitMQ cluster, deploy a relay container, integrate worker claim/lease operations, expose the control queue to workers, or implement Kafka/gRPC. Those are separate reviewable increments. Production rollout requires a quorum-capable RabbitMQ cluster, mTLS workload certificates, managed Ed25519 key rotation, queue permissions per capability, and operational DLQ/redelivery monitoring.