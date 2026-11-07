"""Kafka event-log transport for durable DevOps workflow facts.

Commands remain on RabbitMQ. Kafka carries lifecycle, policy, and audit facts
from the PostgreSQL transactional outbox. Consumers use a durable inbox and
commit offsets only after the handler transaction or quarantine write succeeds.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ContractValidationError, validate_workflow_event
from .rabbitmq import (
    DetachedJwsSigner,
    DetachedJwsVerifier,
    MessageAuthenticationError,
    build_mtls_context,
    canonical_event_bytes,
)
from .store import OutboxLeaseLostError, PostgresInbox, PostgresOutboxRelay


LIFECYCLE_TOPIC = "rusa.lifecycle.v1"
POLICY_TOPIC = "rusa.policy.v1"
AUDIT_TOPIC = "rusa.audit.v1"
DEAD_LETTER_TOPIC = "rusa.events.dlq.v1"

_LIFECYCLE_EVENTS = frozenset(
    {
        "task.accepted",
        "task.progress",
        "tool.invocation",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "workflow.blocked",
        "workflow.completed",
    }
)


class EventRoutingError(ValueError):
    """A workflow event is not a publishable Kafka fact."""


class KafkaQuarantineError(RuntimeError):
    """A rejected Kafka record could not be durably quarantined."""


def event_topic_for(event: dict[str, Any]) -> str:
    """Validate an event and derive its topic from its domain type."""
    validate_workflow_event(event)
    event_type = event["type"]
    if event_type == "policy.violation":
        return POLICY_TOPIC
    if event_type == "audit.recorded":
        return AUDIT_TOPIC
    if event_type in _LIFECYCLE_EVENTS:
        return LIFECYCLE_TOPIC
    raise EventRoutingError(
        f"{event_type!r} is a command or unsupported event, not a Kafka fact"
    )


def event_partition_key(event: dict[str, Any]) -> bytes:
    """Stable, unambiguous tenant+run partition key."""
    if not isinstance(event, dict):
        raise TypeError("event must be a JSON object")
    tenant_id = event.get("tenant_id")
    run_id = event.get("run_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("event tenant_id is required")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("event run_id is required")
    return json.dumps(
        [tenant_id, run_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class KafkaEventTopology:
    """Provisioning contract for independently retained event streams."""

    lifecycle_topic: str = LIFECYCLE_TOPIC
    policy_topic: str = POLICY_TOPIC
    audit_topic: str = AUDIT_TOPIC
    dead_letter_topic: str = DEAD_LETTER_TOPIC
    partitions: int = 12
    replication_factor: int = 3
    min_insync_replicas: int = 2
    retention_ms: int = 90 * 24 * 60 * 60 * 1000
    audit_retention_ms: int = 365 * 24 * 60 * 60 * 1000

    def __post_init__(self) -> None:
        names = (
            self.lifecycle_topic,
            self.policy_topic,
            self.audit_topic,
            self.dead_letter_topic,
        )
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("Kafka topic names are required")
        if len(set(names)) != len(names):
            raise ValueError("Kafka topic names must be unique")
        for value, label, maximum in (
            (self.partitions, "partitions", 1024),
            (self.replication_factor, "replication_factor", 9),
            (self.min_insync_replicas, "min_insync_replicas", 9),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{label} is outside the supported range")
        if self.min_insync_replicas > self.replication_factor:
            raise ValueError("min_insync_replicas cannot exceed replication_factor")
        if (
            isinstance(self.retention_ms, bool)
            or isinstance(self.audit_retention_ms, bool)
            or not isinstance(self.retention_ms, int)
            or not isinstance(self.audit_retention_ms, int)
            or self.retention_ms < 60_000
            or self.audit_retention_ms < self.retention_ms
        ):
            raise ValueError("Kafka retention must be at least one minute and audit >= lifecycle")

    @property
    def topics(self) -> tuple[str, ...]:
        return (
            self.lifecycle_topic,
            self.policy_topic,
            self.audit_topic,
        )

    def topic_for(self, event: dict[str, Any]) -> str:
        default = event_topic_for(event)
        return {
            LIFECYCLE_TOPIC: self.lifecycle_topic,
            POLICY_TOPIC: self.policy_topic,
            AUDIT_TOPIC: self.audit_topic,
        }[default]

    def topic_configs(self) -> dict[str, dict[str, str]]:
        """Return broker-admin settings; topic creation is an operator action."""
        common = {
            "cleanup.policy": "delete",
            "min.insync.replicas": str(self.min_insync_replicas),
        }
        return {
            self.lifecycle_topic: {**common, "retention.ms": str(self.retention_ms)},
            self.policy_topic: {**common, "retention.ms": str(self.audit_retention_ms)},
            self.audit_topic: {**common, "retention.ms": str(self.audit_retention_ms)},
            self.dead_letter_topic: {**common, "retention.ms": str(self.audit_retention_ms)},
        }


def _header_values(headers: Sequence[tuple[str, bytes]] | None, name: str) -> list[bytes]:
    return [
        value
        for key, value in (headers or ())
        if key == name and isinstance(value, bytes)
    ]


def _single_header(headers: Sequence[tuple[str, bytes]] | None, name: str) -> bytes:
    values = _header_values(headers, name)
    if len(values) != 1:
        raise MessageAuthenticationError(f"Kafka record must contain exactly one {name} header")
    return values[0]


class KafkaEventPublisher:
    """Idempotent producer wrapper that signs the exact event bytes."""

    def __init__(
        self,
        *,
        producer: Any,
        signer: DetachedJwsSigner,
        topology: KafkaEventTopology | None = None,
    ):
        if producer is None or not callable(getattr(producer, "send_and_wait", None)):
            raise TypeError("producer must expose async send_and_wait")
        self.producer = producer
        self.signer = signer
        self.topology = topology or KafkaEventTopology()

    async def publish(self, event: dict[str, Any]) -> str:
        topic = self.topology.topic_for(event)
        if event["producer"] != self.signer.producer_id:
            raise MessageAuthenticationError(
                "event producer does not match signing workload identity"
            )
        body = canonical_event_bytes(event)
        signature = self.signer.sign(body)
        headers = [
            ("content-type", b"application/json"),
            ("x-rusa-jws", signature.encode("ascii")),
            ("x-rusa-event-type", event["type"].encode("utf-8")),
            ("x-rusa-schema-version", str(event["schema_version"]).encode("ascii")),
        ]
        confirmation = await self.producer.send_and_wait(
            topic,
            value=body,
            key=event_partition_key(event),
            headers=headers,
        )
        if confirmation is None:
            raise RuntimeError("Kafka did not acknowledge event publication")
        return topic


async def connect_kafka_producer_mtls(
    *,
    bootstrap_servers: str | Sequence[str],
    ca_file: str,
    certificate_file: str,
    private_key_file: str,
    request_timeout_ms: int = 30_000,
) -> Any:
    """Start an idempotent Kafka producer using workload mTLS and acks=all."""
    if not bootstrap_servers:
        raise ValueError("Kafka bootstrap servers are required")
    if (
        isinstance(request_timeout_ms, bool)
        or not isinstance(request_timeout_ms, int)
        or not 1_000 <= request_timeout_ms <= 120_000
    ):
        raise ValueError("request_timeout_ms must be between 1000 and 120000")
    try:
        from aiokafka import AIOKafkaProducer
    except ImportError as exc:
        raise RuntimeError("Install rusa-ai[messaging] to use Kafka transport") from exc
    context = build_mtls_context(
        ca_file=ca_file,
        certificate_file=certificate_file,
        private_key_file=private_key_file,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        security_protocol="SSL",
        ssl_context=context,
        enable_idempotence=True,
        acks="all",
        compression_type="gzip",
        request_timeout_ms=request_timeout_ms,
    )
    await producer.start()
    return producer


class KafkaQuarantinePublisher:
    """Copy an invalid record to an access-restricted, durable Kafka DLQ."""

    def __init__(
        self,
        *,
        producer: Any,
        topic: str = DEAD_LETTER_TOPIC,
    ):
        if not topic or not callable(getattr(producer, "send_and_wait", None)):
            raise ValueError("a Kafka producer and quarantine topic are required")
        self.producer = producer
        self.topic = topic

    async def publish(self, record: Any, *, reason_code: str) -> None:
        if record.topic == self.topic:
            raise KafkaQuarantineError("a dead-letter record cannot be quarantined recursively")
        if not reason_code or len(reason_code) > 128:
            raise ValueError("reason_code must contain 1..128 characters")
        headers = list(record.headers or ())
        headers.extend(
            [
                ("x-rusa-quarantine-reason", reason_code.encode("utf-8")),
                ("x-rusa-source-topic", str(record.topic).encode("utf-8")),
                ("x-rusa-source-partition", str(record.partition).encode("ascii")),
                ("x-rusa-source-offset", str(record.offset).encode("ascii")),
            ]
        )
        confirmation = await self.producer.send_and_wait(
            self.topic,
            value=record.value,
            key=record.key,
            headers=headers,
        )
        if confirmation is None:
            raise KafkaQuarantineError("Kafka did not acknowledge the quarantine record")


class KafkaEventConsumer:
    """Verify and deduplicate facts; never exposes a command execution callback."""

    def __init__(
        self,
        *,
        consumer_id: str,
        verifier: DetachedJwsVerifier,
        tenant_authorizer: Callable[[str, str, dict[str, Any]], bool],
        inbox: PostgresInbox,
        handler: Callable[[Any, dict[str, Any]], None],
        quarantine: KafkaQuarantinePublisher,
        topology: KafkaEventTopology | None = None,
        topics: Sequence[str] | None = None,
        max_message_bytes: int = 1_048_576,
    ):
        if not consumer_id or len(consumer_id) > 128:
            raise ValueError("consumer_id must contain 1..128 characters")
        if not callable(tenant_authorizer) or not callable(handler):
            raise TypeError("tenant_authorizer and handler callbacks are required")
        if not callable(getattr(inbox, "process_once", None)):
            raise TypeError("inbox must expose process_once")
        if not callable(getattr(quarantine, "publish", None)):
            raise TypeError("quarantine must expose publish")
        if (
            isinstance(max_message_bytes, bool)
            or not isinstance(max_message_bytes, int)
            or not 1 <= max_message_bytes <= 16_777_216
        ):
            raise ValueError("max_message_bytes must be between 1 byte and 16 MiB")
        self.consumer_id = consumer_id
        self.verifier = verifier
        self.tenant_authorizer = tenant_authorizer
        self.inbox = inbox
        self.handler = handler
        self.quarantine = quarantine
        self.topology = topology or KafkaEventTopology()
        self.topics = tuple(topics or self.topology.topics)
        if not self.topics or not set(self.topics).issubset(self.topology.topics):
            raise ValueError("consumer topics must be a non-empty subset of event topics")
        self.max_message_bytes = max_message_bytes

    async def _quarantine(self, record: Any, reason_code: str) -> str:
        try:
            result = self.quarantine.publish(record, reason_code=reason_code)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            raise KafkaQuarantineError("failed to persist rejected Kafka record") from exc
        return "quarantined"

    async def handle_record(self, record: Any) -> str:
        try:
            if not isinstance(record.value, bytes) or len(record.value) > self.max_message_bytes:
                raise MessageAuthenticationError("Kafka event body is missing or exceeds the size limit")
            event = json.loads(record.value.decode("utf-8"))
            if not isinstance(event, dict):
                raise MessageAuthenticationError("Kafka event body must be a JSON object")
            validate_workflow_event(event)
            expected_topic = self.topology.topic_for(event)
            if record.topic != expected_topic or record.topic not in self.topics:
                raise MessageAuthenticationError("Kafka topic does not match the validated event type")
            if record.key != event_partition_key(event):
                raise MessageAuthenticationError("Kafka partition key does not match tenant and run")
            if _single_header(record.headers, "x-rusa-event-type").decode("utf-8") != event["type"]:
                raise MessageAuthenticationError("Kafka event type header does not match envelope")
            if _single_header(record.headers, "x-rusa-schema-version").decode("ascii") != str(
                event["schema_version"]
            ):
                raise MessageAuthenticationError("Kafka schema version header does not match envelope")
            signature = _single_header(record.headers, "x-rusa-jws").decode("ascii")
            producer_id = self.verifier.verify(record.value, signature)
            if producer_id != event["producer"]:
                raise MessageAuthenticationError("signed producer identity does not match envelope")
        except (
            ContractValidationError,
            EventRoutingError,
            MessageAuthenticationError,
            AttributeError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            return await self._quarantine(record, type(exc).__name__)

        authorized = self.tenant_authorizer(
            self.consumer_id,
            event["tenant_id"],
            event,
        )
        if inspect.isawaitable(authorized):
            authorized = await authorized
        if authorized is not True:
            return await self._quarantine(record, "TenantAuthorizationDenied")

        processed = await asyncio.to_thread(
            self.inbox.process_once,
            tenant_id=event["tenant_id"],
            consumer_id=self.consumer_id,
            event=event,
            handler=self.handler,
        )
        return "processed" if processed else "duplicate"

    async def run(self, consumer: Any) -> None:
        """Commit each offset only after inbox commit or durable quarantine."""
        await consumer.start()
        try:
            async for record in consumer:
                await self.handle_record(record)
                await consumer.commit()
        finally:
            await consumer.stop()


async def connect_kafka_consumer_mtls(
    *,
    bootstrap_servers: str | Sequence[str],
    group_id: str,
    topics: Sequence[str],
    ca_file: str,
    certificate_file: str,
    private_key_file: str,
    auto_offset_reset: str = "earliest",
) -> Any:
    """Start a manual-commit, read-committed Kafka consumer over workload mTLS."""
    if not bootstrap_servers or not group_id or not topics:
        raise ValueError("Kafka bootstrap servers, group_id, and topics are required")
    if auto_offset_reset not in {"earliest", "latest", "none"}:
        raise ValueError("auto_offset_reset must be earliest, latest, or none")
    try:
        from aiokafka import AIOKafkaConsumer
    except ImportError as exc:
        raise RuntimeError("Install rusa-ai[messaging] to use Kafka transport") from exc
    context = build_mtls_context(
        ca_file=ca_file,
        certificate_file=certificate_file,
        private_key_file=private_key_file,
    )
    consumer = AIOKafkaConsumer(
        *topics,
        bootstrap_servers=bootstrap_servers,
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset=auto_offset_reset,
        isolation_level="read_committed",
        security_protocol="SSL",
        ssl_context=context,
    )
    await consumer.start()
    return consumer


@dataclass(frozen=True)
class KafkaDispatchReport:
    claimed: int
    published: int
    retried: int
    dead_lettered: int
    lease_lost: int


class KafkaOutboxDispatcher:
    """Relay event outbox rows to Kafka; settle only after acks=all response."""

    def __init__(
        self,
        *,
        relay: PostgresOutboxRelay,
        publisher: KafkaEventPublisher,
        max_attempts: int = 8,
        base_delay_seconds: int = 1,
        max_delay_seconds: int = 60,
        jitter: Callable[[int, int], int] = random.randint,
    ):
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if (
            isinstance(base_delay_seconds, bool)
            or isinstance(max_delay_seconds, bool)
            or not 0 <= base_delay_seconds <= max_delay_seconds <= 86400
        ):
            raise ValueError("invalid outbox retry delay bounds")
        self.relay = relay
        self.publisher = publisher
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter = jitter

    async def dispatch_once(self, *, limit: int = 50, lease_seconds: int = 30) -> KafkaDispatchReport:
        rows = await asyncio.to_thread(
            self.relay.claim,
            destination="events",
            limit=limit,
            lease_seconds=lease_seconds,
        )
        published = retried = dead_lettered = lease_lost = 0
        for row in rows:
            try:
                await self.publisher.publish(row["envelope"])
            except Exception as exc:
                exponent = min(max(row["attempts"] - 1, 0), 20)
                delay_cap = min(
                    self.max_delay_seconds,
                    self.base_delay_seconds * (2**exponent),
                )
                delay = self.jitter(0, delay_cap) if delay_cap else 0
                if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= delay_cap:
                    raise ValueError("retry jitter must return an integer within the backoff cap")
                try:
                    status = await asyncio.to_thread(
                        self.relay.retry,
                        outbox_id=row["outbox_id"],
                        lease_token=row["lease_token"],
                        error=type(exc).__name__,
                        delay_seconds=delay,
                        max_attempts=self.max_attempts,
                    )
                except OutboxLeaseLostError:
                    lease_lost += 1
                    continue
                if status == "dead_letter":
                    dead_lettered += 1
                else:
                    retried += 1
                continue

            try:
                await asyncio.to_thread(
                    self.relay.mark_published,
                    outbox_id=row["outbox_id"],
                    lease_token=row["lease_token"],
                )
            except OutboxLeaseLostError:
                # Kafka may have committed; stable message_id and inbox dedup make replay safe.
                lease_lost += 1
            else:
                published += 1

        return KafkaDispatchReport(
            claimed=len(rows),
            published=published,
            retried=retried,
            dead_lettered=dead_lettered,
            lease_lost=lease_lost,
        )
