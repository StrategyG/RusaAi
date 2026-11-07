"""RabbitMQ command transport for durable DevOps workflow messages.

Commands are routed from validated event envelopes. The transport is at-least-once:
publish confirms settle the PostgreSQL outbox, consumers ACK only after their
handler has committed its durable inbox/state transaction, and quorum queues
bound redelivery before dead-lettering poison messages.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import random
import re
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .contracts import ContractValidationError, validate_workflow_event
from .store import OutboxLeaseLostError, PostgresOutboxRelay


PRIORITIES = ("P1", "P2", "P3")
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class CommandRoutingError(ValueError):
    """A valid workflow event is not routable to this command topology."""


class MessageAuthenticationError(ValueError):
    """A signed command does not match its trusted producer or tenant context."""


def canonical_event_bytes(event: dict[str, Any]) -> bytes:
    """Serialize an event deterministically for detached signing and transport."""
    validate_workflow_event(event)
    return json.dumps(
        event,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise MessageAuthenticationError("malformed detached JWS segment")
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


class DetachedJwsSigner:
    """RFC 7797 detached JWS signer using an Ed25519 workload key."""

    def __init__(
        self,
        *,
        kid: str,
        producer_id: str,
        private_key: Ed25519PrivateKey,
    ):
        if not kid or len(kid) > 128 or not producer_id or len(producer_id) > 128:
            raise ValueError("signing key ID and producer ID must contain 1..128 characters")
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("private_key must be an Ed25519 private key")
        self.kid = kid
        self.producer_id = producer_id
        self.private_key = private_key

    @classmethod
    def from_pem(
        cls,
        *,
        kid: str,
        producer_id: str,
        private_key_pem: bytes,
        password: bytes | None = None,
    ) -> DetachedJwsSigner:
        key = serialization.load_pem_private_key(private_key_pem, password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("the configured JWS private key must use Ed25519")
        return cls(kid=kid, producer_id=producer_id, private_key=key)

    def sign(self, payload: bytes) -> str:
        if not isinstance(payload, bytes):
            raise TypeError("detached JWS payload must be bytes")
        protected = {
            "alg": "EdDSA",
            "b64": False,
            "crit": ["b64"],
            "kid": self.kid,
        }
        protected_segment = _b64url_encode(
            json.dumps(
                protected,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signing_input = protected_segment.encode("ascii") + b"." + payload
        signature = self.private_key.sign(signing_input)
        # RFC 7797 compact detached serialization: protected..signature.
        return f"{protected_segment}..{_b64url_encode(signature)}"


class DetachedJwsVerifier:
    """Verify detached Ed25519 JWS and map each key to one producer identity."""

    def __init__(
        self,
        trusted_keys: Mapping[str, tuple[Ed25519PublicKey, str]],
    ):
        if not trusted_keys:
            raise ValueError("at least one trusted producer key is required")
        normalized: dict[str, tuple[Ed25519PublicKey, str]] = {}
        for kid, (public_key, producer_id) in trusted_keys.items():
            if not kid or not producer_id:
                raise ValueError("trusted key IDs and producer IDs must be non-empty")
            if not isinstance(public_key, Ed25519PublicKey):
                raise TypeError("trusted public keys must use Ed25519")
            normalized[kid] = (public_key, producer_id)
        self.trusted_keys = normalized

    @classmethod
    def from_pem(
        cls,
        trusted_keys: Mapping[str, tuple[bytes, str]],
    ) -> DetachedJwsVerifier:
        parsed: dict[str, tuple[Ed25519PublicKey, str]] = {}
        for kid, (public_key_pem, producer_id) in trusted_keys.items():
            key = serialization.load_pem_public_key(public_key_pem)
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError("the configured JWS public keys must use Ed25519")
            parsed[kid] = (key, producer_id)
        return cls(parsed)

    def verify(self, payload: bytes, detached_jws: str) -> str:
        if not isinstance(payload, bytes):
            raise TypeError("detached JWS payload must be bytes")
        if not isinstance(detached_jws, str):
            raise MessageAuthenticationError("detached JWS header is missing")
        parts = detached_jws.split(".")
        if len(parts) != 3 or parts[1] != "":
            raise MessageAuthenticationError("expected detached compact JWS serialization")
        try:
            protected_bytes = _b64url_decode(parts[0])
            protected = json.loads(protected_bytes)
            signature = _b64url_decode(parts[2])
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise MessageAuthenticationError("malformed detached JWS") from exc

        if (
            not isinstance(protected, dict)
            or set(protected) != {"alg", "b64", "crit", "kid"}
            or protected.get("alg") != "EdDSA"
            or protected.get("b64") is not False
            or protected.get("crit") != ["b64"]
            or not isinstance(protected.get("kid"), str)
        ):
            raise MessageAuthenticationError("unsupported or unsafe detached JWS header")

        trusted = self.trusted_keys.get(protected["kid"])
        if trusted is None:
            raise MessageAuthenticationError("detached JWS key ID is not trusted")
        public_key, producer_id = trusted
        signing_input = parts[0].encode("ascii") + b"." + payload
        try:
            public_key.verify(signature, signing_input)
        except InvalidSignature as exc:
            raise MessageAuthenticationError("detached JWS signature verification failed") from exc
        return producer_id


@dataclass(frozen=True)
class CommandRoute:
    routing_key: str
    queue_name: str
    dead_letter_routing_key: str
    dead_letter_queue: str
    priority: str
    capability: str | None


def command_route(
    event: dict[str, Any],
    *,
    allowed_capabilities: Sequence[str],
) -> CommandRoute:
    """Derive a safe command route from a validated event, never from AMQP headers."""
    validate_workflow_event(event)
    if event["type"] == "task.cancel.requested":
        return CommandRoute(
            routing_key="control.p0",
            queue_name="rusa.commands.control.p0",
            dead_letter_routing_key="control.p0.dlq",
            dead_letter_queue="rusa.commands.control.p0.dlq",
            priority="P0",
            capability=None,
        )
    if event["type"] != "task.request":
        raise CommandRoutingError("only task.request and task.cancel.requested are commands")

    capability = event["payload"]["capability"]
    priority = event["payload"]["priority"]
    if not isinstance(capability, str) or not _CAPABILITY_PATTERN.fullmatch(capability):
        raise CommandRoutingError("task capability is not a safe routing identifier")
    if capability not in set(allowed_capabilities):
        raise CommandRoutingError("task capability is not registered in this broker topology")
    if priority not in PRIORITIES:
        raise CommandRoutingError("task priority is not a supported command class")

    suffix = f"{capability}.{priority.lower()}"
    return CommandRoute(
        routing_key=f"task.{suffix}",
        queue_name=f"rusa.commands.{suffix}",
        dead_letter_routing_key=f"task.{suffix}.dlq",
        dead_letter_queue=f"rusa.commands.{suffix}.dlq",
        priority=priority,
        capability=capability,
    )


@dataclass(frozen=True)
class DeclaredCommandTopology:
    exchange: Any
    queues: Mapping[str, Any]
    routes: tuple[CommandRoute, ...]


@dataclass(frozen=True)
class RabbitMQTopology:
    """Durable capability/priority queues plus an isolated P0 control queue."""

    capabilities: tuple[str, ...]
    max_deliveries: int = 5
    exchange_name: str = "rusa.commands.v1"
    dead_letter_exchange_name: str = "rusa.commands.dlx.v1"

    def __post_init__(self) -> None:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("broker capabilities must be unique")
        for capability in self.capabilities:
            if not _CAPABILITY_PATTERN.fullmatch(capability):
                raise ValueError(f"invalid broker capability: {capability!r}")
        if isinstance(self.max_deliveries, bool) or self.max_deliveries < 1:
            raise ValueError("max_deliveries must be a positive integer")
        if not self.exchange_name or not self.dead_letter_exchange_name:
            raise ValueError("RabbitMQ exchange names are required")

    @property
    def routes(self) -> tuple[CommandRoute, ...]:
        routes = [
            CommandRoute(
                routing_key="control.p0",
                queue_name="rusa.commands.control.p0",
                dead_letter_routing_key="control.p0.dlq",
                dead_letter_queue="rusa.commands.control.p0.dlq",
                priority="P0",
                capability=None,
            )
        ]
        for capability in sorted(self.capabilities):
            for priority in PRIORITIES:
                suffix = f"{capability}.{priority.lower()}"
                routes.append(
                    CommandRoute(
                        routing_key=f"task.{suffix}",
                        queue_name=f"rusa.commands.{suffix}",
                        dead_letter_routing_key=f"task.{suffix}.dlq",
                        dead_letter_queue=f"rusa.commands.{suffix}.dlq",
                        priority=priority,
                        capability=capability,
                    )
                )
        return tuple(routes)

    async def declare(self, channel: Any) -> DeclaredCommandTopology:
        try:
            import aio_pika
        except ImportError as exc:
            raise RuntimeError("Install rusa-ai[messaging] to use RabbitMQ transport") from exc

        exchange = await channel.declare_exchange(
            self.exchange_name,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        dead_letter_exchange = await channel.declare_exchange(
            self.dead_letter_exchange_name,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        queues: dict[str, Any] = {}
        for route in self.routes:
            queue = await channel.declare_queue(
                route.queue_name,
                durable=True,
                arguments={
                    "x-queue-type": "quorum",
                    "x-delivery-limit": self.max_deliveries,
                    "x-dead-letter-exchange": self.dead_letter_exchange_name,
                    "x-dead-letter-routing-key": route.dead_letter_routing_key,
                    "x-dead-letter-strategy": "at-least-once",
                    "x-overflow": "reject-publish",
                },
            )
            await queue.bind(exchange, routing_key=route.routing_key)
            dead_letter_queue = await channel.declare_queue(
                route.dead_letter_queue,
                durable=True,
                arguments={"x-queue-type": "quorum"},
            )
            await dead_letter_queue.bind(
                dead_letter_exchange,
                routing_key=route.dead_letter_routing_key,
            )
            queues[route.queue_name] = queue
            queues[route.dead_letter_queue] = dead_letter_queue
        return DeclaredCommandTopology(exchange=exchange, queues=queues, routes=self.routes)


def build_mtls_context(
    *,
    ca_file: str,
    certificate_file: str,
    private_key_file: str,
) -> ssl.SSLContext:
    """Create a verifying client TLS context; broker connections must use AMQPS."""
    if not ca_file or not certificate_file or not private_key_file:
        raise ValueError("CA, client certificate, and client private key are all required")
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_cert_chain(certfile=certificate_file, keyfile=private_key_file)
    return context


async def connect_rabbitmq_mtls(
    *,
    url: str,
    ca_file: str,
    certificate_file: str,
    private_key_file: str,
    timeout_seconds: float = 10.0,
) -> Any:
    """Connect with workload mTLS; plaintext AMQP is deliberately not supported here."""
    if urlsplit(url).scheme != "amqps":
        raise ValueError("RabbitMQ transport requires an amqps:// URL")
    if not 0 < timeout_seconds <= 60:
        raise ValueError("RabbitMQ connection timeout must be in (0, 60] seconds")
    try:
        import aio_pika
    except ImportError as exc:
        raise RuntimeError("Install rusa-ai[messaging] to use RabbitMQ transport") from exc
    context = build_mtls_context(
        ca_file=ca_file,
        certificate_file=certificate_file,
        private_key_file=private_key_file,
    )
    return await aio_pika.connect_robust(
        url,
        ssl_context=context,
        timeout=timeout_seconds,
    )


class RabbitMQCommandPublisher:
    """Persistent signed publisher; the channel must enable publisher confirms."""

    def __init__(
        self,
        *,
        exchange: Any,
        signer: DetachedJwsSigner,
        allowed_capabilities: Sequence[str],
    ):
        self.exchange = exchange
        self.signer = signer
        self.allowed_capabilities = tuple(allowed_capabilities)

    async def publish(self, event: dict[str, Any]) -> CommandRoute:
        try:
            import aio_pika
        except ImportError as exc:
            raise RuntimeError("Install rusa-ai[messaging] to use RabbitMQ transport") from exc

        route = command_route(event, allowed_capabilities=self.allowed_capabilities)
        if event["producer"] != self.signer.producer_id:
            raise MessageAuthenticationError("event producer does not match signing workload identity")
        body = canonical_event_bytes(event)
        signature = self.signer.sign(body)
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=event["message_id"],
            correlation_id=event["correlation_id"],
            headers={
                "x-rusa-jws": signature,
                "x-rusa-routing-key": route.routing_key,
                "x-rusa-event-type": event["type"],
                "x-rusa-schema-version": event["schema_version"],
            },
        )
        confirmation = await self.exchange.publish(
            message,
            routing_key=route.routing_key,
            mandatory=True,
        )
        if (
            confirmation is None
            or confirmation is False
            or confirmation.__class__.__name__ in {"Nack", "Reject"}
        ):
            raise RuntimeError("RabbitMQ did not confirm command publication")
        return route


class RabbitMQCommandConsumer:
    """Manual-ACK consumer; handler completion must mean durable DB commit."""

    def __init__(
        self,
        *,
        consumer_id: str,
        verifier: DetachedJwsVerifier,
        allowed_capabilities: Sequence[str],
        tenant_authorizer: Callable[[str, str, dict[str, Any]], bool],
        handler: Callable[[dict[str, Any]], Any],
        prefetch_count: int = 16,
    ):
        if not consumer_id or len(consumer_id) > 128:
            raise ValueError("consumer_id must contain 1..128 characters")
        if not callable(tenant_authorizer) or not callable(handler):
            raise TypeError("tenant_authorizer and handler callbacks are required")
        if isinstance(prefetch_count, bool) or not 1 <= prefetch_count <= 1024:
            raise ValueError("prefetch_count must be between 1 and 1024")
        self.consumer_id = consumer_id
        self.verifier = verifier
        self.allowed_capabilities = tuple(allowed_capabilities)
        self.tenant_authorizer = tenant_authorizer
        self.handler = handler
        self.prefetch_count = prefetch_count

    async def handle_message(self, message: Any) -> str:
        try:
            event = json.loads(message.body.decode("utf-8"))
            if not isinstance(event, dict):
                raise MessageAuthenticationError("command body must be a JSON object")
            validate_workflow_event(event)
            route = command_route(
                event,
                allowed_capabilities=self.allowed_capabilities,
            )
            headers = message.headers or {}
            if message.message_id != event["message_id"]:
                raise MessageAuthenticationError("AMQP message_id does not match event message_id")
            if message.correlation_id != event["correlation_id"]:
                raise MessageAuthenticationError("AMQP correlation_id does not match event")
            if message.routing_key != route.routing_key:
                raise MessageAuthenticationError("AMQP routing key does not match validated event")
            if headers.get("x-rusa-routing-key") != route.routing_key:
                raise MessageAuthenticationError("signed event route metadata does not match envelope")
            if headers.get("x-rusa-event-type") != event["type"]:
                raise MessageAuthenticationError("event type header does not match envelope")
            if headers.get("x-rusa-schema-version") != event["schema_version"]:
                raise MessageAuthenticationError("schema version header does not match envelope")
            producer_id = self.verifier.verify(
                message.body,
                headers.get("x-rusa-jws"),
            )
            if producer_id != event["producer"]:
                raise MessageAuthenticationError("signed producer identity does not match envelope")
            authorized = self.tenant_authorizer(
                self.consumer_id,
                event["tenant_id"],
                event,
            )
            if inspect.isawaitable(authorized):
                authorized = await authorized
            if authorized is not True:
                raise MessageAuthenticationError("consumer is not authorized for event tenant")
        except (
            ContractValidationError,
            CommandRoutingError,
            MessageAuthenticationError,
            AttributeError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            await message.reject(requeue=False)
            return "rejected"

        try:
            handled = self.handler(event)
            if inspect.isawaitable(handled):
                await handled
        except Exception:
            # Quorum queue delivery-limit moves repeatedly failing commands to the DLQ.
            await message.nack(requeue=True)
            return "requeued"

        # The handler must return only after its inbox/state transaction has committed.
        await message.ack()
        return "acked"

    async def start(
        self,
        *,
        channel: Any,
        topology: RabbitMQTopology,
        queue_names: Sequence[str],
    ) -> tuple[str, ...]:
        declared = await topology.declare(channel)
        await channel.set_qos(prefetch_count=self.prefetch_count)
        consumer_tags: list[str] = []
        for queue_name in queue_names:
            queue = declared.queues.get(queue_name)
            if queue is None or queue_name.endswith(".dlq"):
                raise ValueError(f"queue is not a configured command queue: {queue_name}")
            consumer_tags.append(await queue.consume(self.handle_message, no_ack=False))
        return tuple(consumer_tags)


@dataclass(frozen=True)
class DispatchReport:
    claimed: int
    published: int
    retried: int
    dead_lettered: int
    lease_lost: int


class RabbitMQOutboxDispatcher:
    """Publish claimed command outbox rows and settle only after broker confirms."""

    def __init__(
        self,
        *,
        relay: PostgresOutboxRelay,
        publisher: RabbitMQCommandPublisher,
        max_attempts: int = 8,
        base_delay_seconds: int = 1,
        max_delay_seconds: int = 60,
        jitter: Callable[[int, int], int] = random.randint,
    ):
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if not 0 <= base_delay_seconds <= max_delay_seconds <= 86400:
            raise ValueError("invalid outbox retry delay bounds")
        self.relay = relay
        self.publisher = publisher
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter = jitter

    async def dispatch_once(self, *, limit: int = 50, lease_seconds: int = 30) -> DispatchReport:
        rows = await asyncio.to_thread(
            self.relay.claim,
            destination="commands",
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
                # A broker confirm may have succeeded; stable message_id makes replay safe.
                lease_lost += 1
            else:
                published += 1

        return DispatchReport(
            claimed=len(rows),
            published=published,
            retried=retried,
            dead_lettered=dead_lettered,
            lease_lost=lease_lost,
        )
