"""Unit tests for signed, tenant-aware RabbitMQ command transport."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import aio_pika
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rusa_agents.devops.rabbitmq import (
    CommandRoutingError,
    DetachedJwsSigner,
    DetachedJwsVerifier,
    MessageAuthenticationError,
    RabbitMQCommandConsumer,
    RabbitMQCommandPublisher,
    RabbitMQOutboxDispatcher,
    RabbitMQTopology,
    canonical_event_bytes,
    command_route,
    connect_rabbitmq_mtls,
)


def _task_request(
    *,
    producer: str = "rusa_devops_orchestrator",
    tenant_id: str | None = None,
    capability: str = "research",
    priority: str = "P2",
) -> dict:
    return {
        "schema_version": 1,
        "type": "task.request",
        "message_id": str(uuid4()),
        "tenant_id": tenant_id or f"tenant-{uuid4()}",
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "attempt": 1,
        "correlation_id": str(uuid4()),
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "producer": producer,
        "occurred_at": "2026-11-08T00:00:00Z",
        "payload": {
            "capability": capability,
            "input_ref": "artifact://test/input",
            "deadline_at": "2026-11-08T00:10:00Z",
            "idempotency_key": "test-run/task/1",
            "priority": priority,
        },
    }


def _cancel_request() -> dict:
    event = _task_request()
    event["type"] = "task.cancel.requested"
    event["payload"] = {
        "cancel_epoch": 1,
        "reason_code": "operator_cancelled",
    }
    return event


def _signing_pair(producer: str = "rusa_devops_orchestrator"):
    private_key = Ed25519PrivateKey.generate()
    signer = DetachedJwsSigner(
        kid="test-key-1",
        producer_id=producer,
        private_key=private_key,
    )
    verifier = DetachedJwsVerifier(
        {"test-key-1": (private_key.public_key(), producer)}
    )
    return signer, verifier


def test_task_request_routes_to_registered_capability_and_priority():
    route = command_route(
        _task_request(capability="research", priority="P2"),
        allowed_capabilities=("research", "code"),
    )
    assert route.routing_key == "task.research.p2"
    assert route.queue_name == "rusa.commands.research.p2"
    assert route.dead_letter_queue == "rusa.commands.research.p2.dlq"
    assert route.priority == "P2"


def test_cancel_command_always_uses_isolated_p0_control_queue():
    route = command_route(
        _cancel_request(),
        allowed_capabilities=(),
    )
    assert route.routing_key == "control.p0"
    assert route.queue_name == "rusa.commands.control.p0"
    assert route.priority == "P0"


@pytest.mark.parametrize(
    "event,capabilities",
    [
        (_task_request(capability="../admin"), ("../admin",)),
        (_task_request(capability="unregistered"), ("research",)),
    ],
)
def test_unregistered_or_unsafe_capabilities_are_rejected(event, capabilities):
    with pytest.raises(CommandRoutingError):
        command_route(event, allowed_capabilities=capabilities)


def test_non_command_workflow_events_cannot_enter_rabbitmq_command_exchange():
    event = _task_request()
    event["type"] = "task.progress"
    event["payload"] = {
        "lease_epoch": 1,
        "state_version": 2,
        "percent": 50,
        "stage": "code",
    }
    with pytest.raises(CommandRoutingError):
        command_route(event, allowed_capabilities=("research",))


def test_detached_jws_signs_exact_canonical_payload_and_binds_producer():
    signer, verifier = _signing_pair()
    body = canonical_event_bytes(_task_request())
    signature = signer.sign(body)

    assert signature.count(".") == 2
    assert signature.split(".")[1] == ""
    assert verifier.verify(body, signature) == "rusa_devops_orchestrator"
    with pytest.raises(MessageAuthenticationError, match="signature verification failed"):
        verifier.verify(body + b" ", signature)


def test_detached_jws_rejects_unknown_key_and_unsafe_header():
    signer, verifier = _signing_pair()
    body = canonical_event_bytes(_task_request())
    signature = signer.sign(body)
    with pytest.raises(MessageAuthenticationError, match="not trusted"):
        DetachedJwsVerifier(
            {"different-key": (signer.private_key.public_key(), signer.producer_id)}
        ).verify(body, signature)
    with pytest.raises(MessageAuthenticationError):
        verifier.verify(body, "not.a.jws")


class _FakeExchange:
    def __init__(self, confirmation):
        self.confirmation = confirmation
        self.message = None
        self.kwargs = None

    async def publish(self, message, **kwargs):
        self.message = message
        self.kwargs = kwargs
        return self.confirmation


class _Nack:
    pass


class _Ack:
    pass


@pytest.mark.asyncio
async def test_publisher_sends_persistent_signed_message_and_requires_confirm():
    signer, verifier = _signing_pair()
    exchange = _FakeExchange(_Ack())
    publisher = RabbitMQCommandPublisher(
        exchange=exchange,
        signer=signer,
        allowed_capabilities=("research",),
    )
    event = _task_request()
    route = await publisher.publish(event)

    assert route.routing_key == "task.research.p2"
    assert exchange.kwargs == {"routing_key": "task.research.p2", "mandatory": True}
    assert exchange.message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert exchange.message.message_id == event["message_id"]
    assert exchange.message.correlation_id == event["correlation_id"]
    assert verifier.verify(
        exchange.message.body,
        exchange.message.headers["x-rusa-jws"],
    ) == event["producer"]
    assert exchange.message.headers["x-rusa-routing-key"] == route.routing_key


@pytest.mark.asyncio
async def test_publisher_rejects_wrong_producer_and_missing_publisher_confirm():
    signer, _ = _signing_pair()
    event = _task_request(producer="another_service")
    publisher = RabbitMQCommandPublisher(
        exchange=_FakeExchange(_Ack()),
        signer=signer,
        allowed_capabilities=("research",),
    )
    with pytest.raises(MessageAuthenticationError, match="producer"):
        await publisher.publish(event)

    publisher = RabbitMQCommandPublisher(
        exchange=_FakeExchange(None),
        signer=signer,
        allowed_capabilities=("research",),
    )
    with pytest.raises(RuntimeError, match="did not confirm"):
        await publisher.publish(_task_request())


class _FakeIncomingMessage:
    def __init__(self, event, signer, *, tamper_signature=False):
        self.body = canonical_event_bytes(event)
        self.message_id = event["message_id"]
        self.correlation_id = event["correlation_id"]
        self.routing_key = "task.research.p2"
        signature = signer.sign(self.body)
        if tamper_signature:
            signature = signature[:-1] + ("A" if signature[-1] != "A" else "B")
        self.headers = {
            "x-rusa-jws": signature,
            "x-rusa-routing-key": self.routing_key,
            "x-rusa-event-type": event["type"],
            "x-rusa-schema-version": event["schema_version"],
        }
        self.actions = []

    async def ack(self):
        self.actions.append(("ack",))

    async def reject(self, *, requeue):
        self.actions.append(("reject", requeue))

    async def nack(self, *, requeue):
        self.actions.append(("nack", requeue))


@pytest.mark.asyncio
async def test_consumer_acknowledges_only_after_durable_handler_returns():
    signer, verifier = _signing_pair()
    message = _FakeIncomingMessage(_task_request(), signer)
    order = []

    async def handler(event):
        await asyncio.sleep(0)
        order.append(("commit", event["message_id"]))

    consumer = RabbitMQCommandConsumer(
        consumer_id="research-worker",
        verifier=verifier,
        allowed_capabilities=("research",),
        tenant_authorizer=lambda consumer, tenant, event: consumer == "research-worker",
        handler=handler,
    )
    assert await consumer.handle_message(message) == "acked"
    assert order == [("commit", message.message_id)]
    assert message.actions == [("ack",)]


@pytest.mark.asyncio
async def test_consumer_rejects_bad_signature_or_tenant_and_requeues_handler_failure():
    signer, verifier = _signing_pair()
    consumer = RabbitMQCommandConsumer(
        consumer_id="research-worker",
        verifier=verifier,
        allowed_capabilities=("research",),
        tenant_authorizer=lambda consumer, tenant, event: tenant != "blocked-tenant",
        handler=lambda event: None,
    )
    bad_signature = _FakeIncomingMessage(_task_request(), signer, tamper_signature=True)
    assert await consumer.handle_message(bad_signature) == "rejected"
    assert bad_signature.actions == [("reject", False)]

    blocked_event = _task_request(tenant_id="blocked-tenant")
    blocked = _FakeIncomingMessage(blocked_event, signer)
    assert await consumer.handle_message(blocked) == "rejected"
    assert blocked.actions == [("reject", False)]

    failing = _FakeIncomingMessage(_task_request(), signer)

    def fail_handler(event):
        raise RuntimeError("database temporarily unavailable")

    retrying_consumer = RabbitMQCommandConsumer(
        consumer_id="research-worker",
        verifier=verifier,
        allowed_capabilities=("research",),
        tenant_authorizer=lambda consumer, tenant, event: True,
        handler=fail_handler,
    )
    assert await retrying_consumer.handle_message(failing) == "requeued"
    assert failing.actions == [("nack", True)]


class _FakeQueue:
    def __init__(self, name):
        self.name = name
        self.bindings = []
        self.consumer = None

    async def bind(self, exchange, *, routing_key):
        self.bindings.append((exchange.name, routing_key))

    async def consume(self, callback, *, no_ack):
        self.consumer = (callback, no_ack)
        return f"consumer-{self.name}"


class _FakeDeclaredExchange:
    def __init__(self, name):
        self.name = name


class _FakeChannel:
    def __init__(self):
        self.exchanges = {}
        self.queues = {}
        self.queue_arguments = {}
        self.prefetch = None

    async def declare_exchange(self, name, *, type, durable):
        assert type == aio_pika.ExchangeType.DIRECT
        assert durable is True
        exchange = _FakeDeclaredExchange(name)
        self.exchanges[name] = exchange
        return exchange

    async def declare_queue(self, name, *, durable, arguments):
        assert durable is True
        queue = _FakeQueue(name)
        self.queues[name] = queue
        self.queue_arguments[name] = arguments
        return queue

    async def set_qos(self, *, prefetch_count):
        self.prefetch = prefetch_count


@pytest.mark.asyncio
async def test_topology_declares_quorum_queues_per_capability_and_priority_with_dlq():
    channel = _FakeChannel()
    topology = RabbitMQTopology(capabilities=("research", "code"))
    declared = await topology.declare(channel)

    assert len(declared.routes) == 7  # P0 plus three priorities for each capability.
    assert len(channel.queues) == 14  # Every command queue has its own DLQ.
    primary = channel.queue_arguments["rusa.commands.research.p2"]
    assert primary["x-queue-type"] == "quorum"
    assert primary["x-delivery-limit"] == 5
    assert primary["x-dead-letter-strategy"] == "at-least-once"
    assert channel.queues["rusa.commands.research.p2"].bindings == [
        ("rusa.commands.v1", "task.research.p2")
    ]


class _FakeRelay:
    def __init__(self, rows):
        self.rows = rows
        self.published = []
        self.retries = []

    def claim(self, **kwargs):
        assert kwargs["destination"] == "commands"
        return self.rows

    def mark_published(self, **kwargs):
        self.published.append(kwargs)

    def retry(self, **kwargs):
        self.retries.append(kwargs)
        return "pending"


class _FakePublisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        if self.fail:
            raise TimeoutError("broker publish confirmation timed out")


@pytest.mark.asyncio
async def test_outbox_dispatcher_marks_published_only_after_confirm():
    event = _task_request()
    row = {
        "outbox_id": 7,
        "lease_token": "lease-1",
        "attempts": 1,
        "envelope": event,
    }
    relay = _FakeRelay([row])
    publisher = _FakePublisher()
    dispatcher = RabbitMQOutboxDispatcher(
        relay=relay,
        publisher=publisher,
        jitter=lambda low, high: low,
    )
    report = await dispatcher.dispatch_once()

    assert report.claimed == 1
    assert report.published == 1
    assert relay.published == [{"outbox_id": 7, "lease_token": "lease-1"}]
    assert relay.retries == []


@pytest.mark.asyncio
async def test_outbox_dispatcher_retries_ambiguous_publish_with_same_message_id():
    event = _task_request()
    row = {
        "outbox_id": 8,
        "lease_token": "lease-2",
        "attempts": 2,
        "envelope": event,
    }
    relay = _FakeRelay([row])
    publisher = _FakePublisher(fail=True)
    dispatcher = RabbitMQOutboxDispatcher(
        relay=relay,
        publisher=publisher,
        jitter=lambda low, high: low,
    )
    report = await dispatcher.dispatch_once()

    assert report.retried == 1
    assert relay.published == []
    assert relay.retries[0]["outbox_id"] == 8
    assert relay.retries[0]["lease_token"] == "lease-2"
    assert relay.retries[0]["delay_seconds"] == 2
    assert publisher.events[0]["message_id"] == event["message_id"]


@pytest.mark.asyncio
async def test_mtls_connection_rejects_plaintext_amqp_url():
    with pytest.raises(ValueError, match="amqps"):
        await connect_rabbitmq_mtls(
            url="amqp://localhost/",
            ca_file="/missing/ca.pem",
            certificate_file="/missing/client.pem",
            private_key_file="/missing/client.key",
        )
