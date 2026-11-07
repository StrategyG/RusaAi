import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rusa_agents.devops.kafka import (
    AUDIT_TOPIC,
    DEAD_LETTER_TOPIC,
    LIFECYCLE_TOPIC,
    POLICY_TOPIC,
    EventRoutingError,
    KafkaEventConsumer,
    KafkaEventPublisher,
    KafkaEventTopology,
    KafkaOutboxDispatcher,
    KafkaQuarantineError,
    KafkaQuarantinePublisher,
    event_partition_key,
    event_topic_for,
)
from rusa_agents.devops.rabbitmq import (
    DetachedJwsSigner,
    DetachedJwsVerifier,
    MessageAuthenticationError,
    canonical_event_bytes,
)


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _progress_event() -> dict:
    return {
        "schema_version": 1,
        "type": "task.progress",
        "message_id": str(uuid4()),
        "tenant_id": f"tenant-{uuid4()}",
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "attempt": 1,
        "correlation_id": str(uuid4()),
        "traceparent": TRACEPARENT,
        "producer": "rusa_devops_orchestrator",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "lease_epoch": 1,
            "state_version": 2,
            "percent": 50,
            "stage": "test",
        },
    }


def _policy_event() -> dict:
    event = _progress_event()
    event["type"] = "policy.violation"
    event.pop("task_id")
    event.pop("attempt")
    event["payload"] = {
        "policy_id": "policy-prod",
        "policy_version": "2026-11",
        "decision_id": str(uuid4()),
        "action": "deployment.execute",
        "resource_ref": "environment://production",
        "reason_code": "approval_required",
    }
    return event


def _audit_event() -> dict:
    event = _progress_event()
    event["type"] = "audit.recorded"
    event.pop("task_id")
    event.pop("attempt")
    event["payload"] = {
        "action": "artifact.publish",
        "outcome": "denied",
        "resource_ref": "artifact://release/123",
    }
    return event


def _signing_pair():
    private_key = Ed25519PrivateKey.generate()
    signer = DetachedJwsSigner(
        kid="orchestrator-key-1",
        producer_id="rusa_devops_orchestrator",
        private_key=private_key,
    )
    verifier = DetachedJwsVerifier(
        {"orchestrator-key-1": (private_key.public_key(), "rusa_devops_orchestrator")}
    )
    return signer, verifier


class FakeProducer:
    def __init__(self, confirmation=object()):
        self.confirmation = confirmation
        self.calls = []

    async def send_and_wait(self, topic, **kwargs):
        self.calls.append((topic, kwargs))
        return self.confirmation


def _record(event, signer, *, topic=None, tamper=False):
    body = canonical_event_bytes(event)
    signature = signer.sign(body)
    if tamper:
        signature = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    return SimpleNamespace(
        topic=topic or event_topic_for(event),
        partition=2,
        offset=17,
        key=event_partition_key(event),
        value=body,
        headers=[
            ("x-rusa-jws", signature.encode("ascii")),
            ("x-rusa-event-type", event["type"].encode()),
            ("x-rusa-schema-version", b"1"),
        ],
    )


class FakeInbox:
    def __init__(self, *, duplicate=False, error=None):
        self.duplicate = duplicate
        self.error = error
        self.calls = []

    def process_once(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        kwargs["handler"]("transaction", kwargs["event"])
        return not self.duplicate


class FakeQuarantine:
    def __init__(self, error=None):
        self.records = []
        self.error = error

    async def publish(self, record, *, reason_code):
        if self.error:
            raise self.error
        self.records.append((record, reason_code))


def _consumer(verifier, inbox=None, quarantine=None, *, authorizer=None, **kwargs):
    return KafkaEventConsumer(
        consumer_id="reporting-projection",
        verifier=verifier,
        tenant_authorizer=authorizer or (lambda *_args: True),
        inbox=inbox or FakeInbox(),
        handler=lambda _conn, _event: None,
        quarantine=quarantine or FakeQuarantine(),
        **kwargs,
    )


def test_event_routing_separates_lifecycle_policy_and_audit_topics():
    assert event_topic_for(_progress_event()) == LIFECYCLE_TOPIC
    assert event_topic_for(_policy_event()) == POLICY_TOPIC
    assert event_topic_for(_audit_event()) == AUDIT_TOPIC


def test_commands_are_not_accepted_as_kafka_facts():
    event = _progress_event()
    event["type"] = "task.request"
    event["payload"] = {
        "capability": "research",
        "input_ref": "artifact://input/1",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        "idempotency_key": "run/task/1",
        "priority": "P2",
    }
    with pytest.raises(EventRoutingError, match="not a Kafka fact"):
        event_topic_for(event)


def test_partition_key_is_tenant_and_run_scoped_and_unambiguous():
    event = _progress_event()
    key = event_partition_key(event)
    assert key == json.dumps(
        [event["tenant_id"], event["run_id"]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    changed = dict(event, tenant_id=event["tenant_id"] + ":other")
    assert event_partition_key(changed) != key


def test_topology_declares_separate_retention_and_replication_settings():
    topology = KafkaEventTopology()
    configs = topology.topic_configs()
    assert set(configs) == {
        LIFECYCLE_TOPIC,
        POLICY_TOPIC,
        AUDIT_TOPIC,
        DEAD_LETTER_TOPIC,
    }
    assert configs[LIFECYCLE_TOPIC]["retention.ms"] == str(topology.retention_ms)
    assert configs[AUDIT_TOPIC]["retention.ms"] == str(topology.audit_retention_ms)
    assert configs[AUDIT_TOPIC]["min.insync.replicas"] == "2"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"partitions": 0},
        {"replication_factor": 1, "min_insync_replicas": 2},
        {"retention_ms": 1000},
        {"lifecycle_topic": "same", "policy_topic": "same"},
    ],
)
def test_topology_rejects_unsafe_topic_configuration(kwargs):
    with pytest.raises(ValueError):
        KafkaEventTopology(**kwargs)


@pytest.mark.asyncio
async def test_publisher_signs_event_and_uses_tenant_run_partition_key():
    signer, verifier = _signing_pair()
    producer = FakeProducer()
    publisher = KafkaEventPublisher(producer=producer, signer=signer)
    event = _progress_event()

    topic = await publisher.publish(event)
    assert topic == LIFECYCLE_TOPIC
    sent_topic, kwargs = producer.calls[0]
    assert sent_topic == LIFECYCLE_TOPIC
    assert kwargs["key"] == event_partition_key(event)
    assert kwargs["value"] == canonical_event_bytes(event)
    headers = dict(kwargs["headers"])
    assert verifier.verify(kwargs["value"], headers["x-rusa-jws"].decode()) == event["producer"]
    assert headers["x-rusa-event-type"] == b"task.progress"


@pytest.mark.asyncio
async def test_publisher_rejects_wrong_producer_and_missing_kafka_ack():
    signer, _ = _signing_pair()
    event = _progress_event()
    event["producer"] = "forged-producer"
    with pytest.raises(MessageAuthenticationError, match="producer"):
        await KafkaEventPublisher(
            producer=FakeProducer(),
            signer=signer,
        ).publish(event)

    with pytest.raises(RuntimeError, match="did not acknowledge"):
        await KafkaEventPublisher(
            producer=FakeProducer(confirmation=None),
            signer=signer,
        ).publish(_progress_event())


@pytest.mark.asyncio
async def test_consumer_verifies_signature_and_commits_through_inbox_once():
    signer, verifier = _signing_pair()
    event = _progress_event()
    record = _record(event, signer)
    calls = []
    inbox = FakeInbox()
    consumer = KafkaEventConsumer(
        consumer_id="reporting-projection",
        verifier=verifier,
        tenant_authorizer=lambda consumer_id, tenant_id, item: (
            consumer_id == "reporting-projection" and tenant_id == item["tenant_id"]
        ),
        inbox=inbox,
        handler=lambda conn, item: calls.append((conn, item["message_id"])),
        quarantine=FakeQuarantine(),
    )

    assert await consumer.handle_record(record) == "processed"
    assert calls == [("transaction", event["message_id"])]
    assert inbox.calls[0]["tenant_id"] == event["tenant_id"]


@pytest.mark.asyncio
async def test_consumer_returns_duplicate_without_reapplying_projection():
    signer, verifier = _signing_pair()
    event = _progress_event()
    inbox = FakeInbox(duplicate=True)
    consumer = _consumer(verifier, inbox=inbox)
    assert await consumer.handle_record(_record(event, signer)) == "duplicate"
    assert len(inbox.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda record: setattr(record, "topic", POLICY_TOPIC), "MessageAuthenticationError"),
        (lambda record: setattr(record, "key", b"wrong-key"), "MessageAuthenticationError"),
        (lambda record: record.headers.append(("x-rusa-jws", b"duplicate")), "MessageAuthenticationError"),
    ],
)
async def test_consumer_quarantines_invalid_topic_key_or_duplicate_security_header(mutate, reason):
    signer, verifier = _signing_pair()
    event = _progress_event()
    record = _record(event, signer)
    mutate(record)
    quarantine = FakeQuarantine()
    consumer = _consumer(verifier, quarantine=quarantine)

    assert await consumer.handle_record(record) == "quarantined"
    assert quarantine.records[0][1] == reason


@pytest.mark.asyncio
async def test_consumer_quarantines_invalid_signature_and_tenant_denial():
    signer, verifier = _signing_pair()
    event = _progress_event()
    bad = _record(event, signer, tamper=True)
    quarantine = FakeQuarantine()
    consumer = _consumer(verifier, quarantine=quarantine)

    assert await consumer.handle_record(bad) == "quarantined"
    assert quarantine.records[0][1] == "MessageAuthenticationError"

    denied_quarantine = FakeQuarantine()
    denied = _consumer(
        verifier,
        quarantine=denied_quarantine,
        authorizer=lambda *_args: False,
    )
    assert await denied.handle_record(_record(event, signer)) == "quarantined"
    assert denied_quarantine.records[0][1] == "TenantAuthorizationDenied"


@pytest.mark.asyncio
async def test_consumer_fails_closed_when_tenant_authorizer_is_unavailable():
    signer, verifier = _signing_pair()

    def unavailable(*_args):
        raise RuntimeError("identity service unavailable")

    consumer = _consumer(verifier, authorizer=unavailable)
    with pytest.raises(RuntimeError, match="identity service unavailable"):
        await consumer.handle_record(_record(_progress_event(), signer))


@pytest.mark.asyncio
async def test_consumer_does_not_ack_when_quarantine_or_inbox_commit_fails():
    signer, verifier = _signing_pair()
    event = _progress_event()
    bad_record = _record(event, signer, tamper=True)
    consumer = _consumer(
        verifier,
        quarantine=FakeQuarantine(error=RuntimeError("DLQ unavailable")),
    )
    with pytest.raises(KafkaQuarantineError):
        await consumer.handle_record(bad_record)

    failing_inbox = FakeInbox(error=RuntimeError("database unavailable"))
    consumer = _consumer(verifier, inbox=failing_inbox)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await consumer.handle_record(_record(event, signer))


@pytest.mark.asyncio
async def test_consumer_commits_offsets_only_after_handler_or_quarantine_finishes():
    signer, verifier = _signing_pair()
    record = _record(_progress_event(), signer)

    class FakeKafkaConsumer:
        def __init__(self):
            self.commits = 0
            self.started = False
            self.stopped = False

        async def start(self):
            self.started = True

        def __aiter__(self):
            async def records():
                yield record
            return records()

        async def commit(self):
            self.commits += 1

        async def stop(self):
            self.stopped = True

    kafka = FakeKafkaConsumer()
    await _consumer(verifier).run(kafka)
    assert kafka.started is True
    assert kafka.commits == 1
    assert kafka.stopped is True


@pytest.mark.asyncio
async def test_consumer_does_not_commit_offset_after_inbox_failure():
    signer, verifier = _signing_pair()
    record = _record(_progress_event(), signer)

    class FakeKafkaConsumer:
        def __init__(self):
            self.commits = 0
            self.stopped = False

        async def start(self):
            pass

        def __aiter__(self):
            async def records():
                yield record
            return records()

        async def commit(self):
            self.commits += 1

        async def stop(self):
            self.stopped = True

    kafka = FakeKafkaConsumer()
    consumer = _consumer(verifier, inbox=FakeInbox(error=RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="db down"):
        await consumer.run(kafka)
    assert kafka.commits == 0
    assert kafka.stopped is True


@pytest.mark.asyncio
async def test_quarantine_publisher_preserves_original_record_and_source_metadata():
    producer = FakeProducer()
    quarantine = KafkaQuarantinePublisher(producer=producer)
    record = SimpleNamespace(
        topic=LIFECYCLE_TOPIC,
        partition=3,
        offset=91,
        key=b"tenant-run",
        value=b'{"invalid":true}',
        headers=[("original-header", b"original")],
    )
    await quarantine.publish(record, reason_code="ContractValidationError")
    topic, kwargs = producer.calls[0]
    assert topic == DEAD_LETTER_TOPIC
    assert kwargs["value"] == record.value
    assert kwargs["key"] == record.key
    headers = dict(kwargs["headers"])
    assert headers["x-rusa-source-topic"] == LIFECYCLE_TOPIC.encode()
    assert headers["x-rusa-source-partition"] == b"3"
    assert headers["x-rusa-source-offset"] == b"91"


@pytest.mark.asyncio
async def test_quarantine_publisher_prevents_recursive_dead_lettering():
    quarantine = KafkaQuarantinePublisher(producer=FakeProducer())
    record = SimpleNamespace(
        topic=DEAD_LETTER_TOPIC,
        partition=0,
        offset=1,
        key=None,
        value=b"invalid",
        headers=[],
    )
    with pytest.raises(KafkaQuarantineError, match="recursively"):
        await quarantine.publish(record, reason_code="Invalid")


class FakeRelay:
    def __init__(self, rows):
        self.rows = rows
        self.published = []
        self.retries = []

    def claim(self, **kwargs):
        assert kwargs["destination"] == "events"
        return self.rows

    def mark_published(self, **kwargs):
        self.published.append(kwargs)

    def retry(self, **kwargs):
        self.retries.append(kwargs)
        return "pending"


class FakeEventPublisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        if self.fail:
            raise TimeoutError("Kafka ack timed out")


@pytest.mark.asyncio
async def test_kafka_outbox_dispatcher_marks_event_published_after_ack():
    event = _progress_event()
    relay = FakeRelay([{
        "outbox_id": 3,
        "lease_token": "lease-3",
        "attempts": 1,
        "envelope": event,
    }])
    publisher = FakeEventPublisher()
    report = await KafkaOutboxDispatcher(
        relay=relay,
        publisher=publisher,
        jitter=lambda low, high: low,
    ).dispatch_once()
    assert report.claimed == 1
    assert report.published == 1
    assert relay.published == [{"outbox_id": 3, "lease_token": "lease-3"}]
    assert relay.retries == []


@pytest.mark.asyncio
async def test_kafka_outbox_dispatcher_retries_without_changing_message_identity():
    event = _progress_event()
    relay = FakeRelay([{
        "outbox_id": 4,
        "lease_token": "lease-4",
        "attempts": 2,
        "envelope": event,
    }])
    publisher = FakeEventPublisher(fail=True)
    report = await KafkaOutboxDispatcher(
        relay=relay,
        publisher=publisher,
        jitter=lambda low, high: low,
    ).dispatch_once()
    assert report.retried == 1
    assert relay.published == []
    assert relay.retries[0]["delay_seconds"] == 2
    assert publisher.events[0]["message_id"] == event["message_id"]
