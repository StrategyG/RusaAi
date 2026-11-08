"""mTLS-authenticated internal gRPC control plane for durable DevOps workflows."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import grpc

from .contracts import ContractValidationError
from .control_store import (
    PostgresWorkflowControlStore,
    RpcReplayConflictError,
    TaskAccessDeniedError,
    TaskStateConflictError,
    WorkflowNotFoundError,
)
from .store import WorkflowIdempotencyConflictError

LOGGER = logging.getLogger("rusa.devops.grpc")
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRINCIPAL_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")

READ_METHODS = frozenset({"GetRun", "GetTask"})
ORCHESTRATOR_METHODS = frozenset({"GetRun", "GetTask", "CancelRun"})
TASK_METHODS = frozenset(
    {
        "GetRun",
        "GetTask",
        "ClaimTask",
        "HeartbeatTask",
        "ReportTaskProgress",
        "ReportTaskResult",
    }
)
STAGE_AGENT_IDS = frozenset(
    {
        "rusa_plan_agent",
        "rusa_code_agent",
        "rusa_build_agent",
        "rusa_test_agent",
        "rusa_release_agent",
        "rusa_deploy_agent",
        "rusa_operate_agent",
        "rusa_monitor_agent",
        "rusa_security_guard_agent",
    }
)

DEFAULT_PRINCIPAL_METHODS = {
    "rusa_supreme_orchestrator": ORCHESTRATOR_METHODS,
    "rusa_devops_orchestrator": ORCHESTRATOR_METHODS,
    **{agent_id: TASK_METHODS for agent_id in STAGE_AGENT_IDS},
    "rusa_system_architect_agent": READ_METHODS,
    "rusa_planner_agent": READ_METHODS,
    "rusa_task_decomposer_agent": READ_METHODS,
    "rusa_software_engineer_agent": READ_METHODS,
    "rusa_devops_agent": READ_METHODS,
    "rusa_qa_testing_agent": READ_METHODS,
    "rusa_security_policy_agent": READ_METHODS,
    "rusa_memory_knowledge_agent": READ_METHODS,
    "rusa_evaluation_reporting_agent": READ_METHODS,
}


class IdentityAuthenticationError(ValueError):
    """A gRPC peer did not present a trusted RUSA workload identity."""


@dataclass(frozen=True)
class WorkloadIdentity:
    tenant_id: str
    principal: str
    allowed_methods: frozenset[str]
    spiffe_id: str


class SpiffeIdentityResolver:
    """Resolve tenant and principal only from a verified mTLS certificate SAN URI.

    Expected URI: spiffe://<trust-domain>/tenant/<tenant-id>/service/<principal>
    Metadata headers are never used as an identity or tenant source.
    """

    def __init__(
        self,
        *,
        trust_domain: str,
        principal_methods: dict[str, frozenset[str]] | None = None,
    ):
        domain = trust_domain.strip().lower()
        if not domain or "/" in domain or ":" in domain:
            raise ValueError("trust_domain must be a bare DNS trust domain")
        self.trust_domain = domain
        method_map = (
            DEFAULT_PRINCIPAL_METHODS
            if principal_methods is None
            else principal_methods
        )
        self.principal_methods = dict(method_map)

    @staticmethod
    def _auth_values(auth_context: dict, key: str) -> list[bytes]:
        values = auth_context.get(key, auth_context.get(key.encode(), []))
        if isinstance(values, (bytes, str)):
            values = [values]
        return [
            value.encode("utf-8") if isinstance(value, str) else value
            for value in values
            if isinstance(value, (bytes, str))
        ]

    def resolve(self, auth_context: dict) -> WorkloadIdentity:
        if not isinstance(auth_context, dict):
            raise IdentityAuthenticationError("peer certificate context is unavailable")
        security_types = self._auth_values(auth_context, "transport_security_type")
        if security_types != [b"ssl"]:
            raise IdentityAuthenticationError("internal gRPC requires mTLS")

        sans = self._auth_values(auth_context, "x509_subject_alternative_name")
        candidates = []
        for raw in sans:
            value = raw.decode("utf-8", errors="strict")
            if value.startswith("URI:"):
                value = value[4:]
            if value.startswith("spiffe://"):
                candidates.append(value)
        if len(candidates) != 1:
            raise IdentityAuthenticationError(
                "peer certificate must contain exactly one RUSA SPIFFE URI SAN"
            )

        spiffe_id = candidates[0]
        parsed = urlsplit(spiffe_id)
        if (
            parsed.scheme != "spiffe"
            or parsed.netloc.lower() != self.trust_domain
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise IdentityAuthenticationError("SPIFFE trust domain is not trusted")

        raw_parts = parsed.path.split("/")
        if (
            len(raw_parts) != 5
            or raw_parts[0] != ""
            or raw_parts[1] != "tenant"
            or not raw_parts[2]
            or raw_parts[3] != "service"
            or not raw_parts[4]
        ):
            raise IdentityAuthenticationError("SPIFFE URI path is not a canonical RUSA workload identity")
        tenant_id, principal = unquote(raw_parts[2]), unquote(raw_parts[4])
        if tenant_id != raw_parts[2] or principal != raw_parts[4]:
            raise IdentityAuthenticationError("SPIFFE URI path segments must not be percent-encoded")
        if not _TENANT_RE.fullmatch(tenant_id):
            raise IdentityAuthenticationError("SPIFFE tenant identifier is invalid")
        if not _PRINCIPAL_RE.fullmatch(principal):
            raise IdentityAuthenticationError("SPIFFE principal identifier is invalid")
        allowed = self.principal_methods.get(principal)
        if allowed is None:
            raise IdentityAuthenticationError("SPIFFE principal is not registered")
        return WorkloadIdentity(
            tenant_id=tenant_id,
            principal=principal,
            allowed_methods=frozenset(allowed),
            spiffe_id=spiffe_id,
        )


class WorkflowControlServicer:
    """Generated-stub-compatible service; all tenant authority comes from mTLS."""

    def __init__(
        self,
        *,
        store: PostgresWorkflowControlStore,
        identity_resolver: SpiffeIdentityResolver,
        rpc_timeout_seconds: float = 2.0,
    ):
        if not callable(getattr(store, "get_run", None)):
            raise TypeError("store must implement the DevOps workflow control interface")
        if isinstance(rpc_timeout_seconds, bool) or not 0.1 <= rpc_timeout_seconds <= 30:
            raise ValueError("rpc_timeout_seconds must be between 0.1 and 30")
        self.store = store
        self.identity_resolver = identity_resolver
        self.rpc_timeout_seconds = rpc_timeout_seconds

    async def _abort(
        self,
        context: Any,
        code: grpc.StatusCode,
        details: str,
    ) -> None:
        await context.abort(code, details)

    async def _identity(
        self,
        context: Any,
        *,
        method: str,
        requested_tenant: str,
    ) -> WorkloadIdentity:
        try:
            identity = self.identity_resolver.resolve(context.auth_context())
        except (IdentityAuthenticationError, UnicodeDecodeError, ValueError):
            await self._abort(
                context,
                grpc.StatusCode.UNAUTHENTICATED,
                "a trusted mTLS workload identity is required",
            )
            raise AssertionError("context.abort must terminate the RPC") from None
        if method not in identity.allowed_methods:
            await self._abort(
                context,
                grpc.StatusCode.PERMISSION_DENIED,
                "workload identity is not authorized for this RPC",
            )
        if requested_tenant != identity.tenant_id:
            await self._abort(
                context,
                grpc.StatusCode.PERMISSION_DENIED,
                "tenant_id does not match the authenticated workload identity",
            )
        return identity

    def _timeout(self, context: Any) -> float:
        remaining = context.time_remaining()
        if remaining is None:
            raise ValueError("every internal RPC must carry an absolute deadline")
        if remaining <= 0:
            raise TimeoutError("RPC deadline has expired")
        return min(float(remaining), self.rpc_timeout_seconds)

    async def _call(
        self,
        context: Any,
        operation,
        **kwargs,
    ):
        try:
            timeout = self._timeout(context)
            return await asyncio.wait_for(
                asyncio.to_thread(operation, **kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._abort(
                context,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                "workflow control operation exceeded its deadline",
            )
        except TimeoutError:
            await self._abort(
                context,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                "RPC deadline is required and must be in the future",
            )
        except WorkflowNotFoundError:
            await self._abort(context, grpc.StatusCode.NOT_FOUND, "workflow or task not found")
        except TaskAccessDeniedError:
            await self._abort(
                context,
                grpc.StatusCode.PERMISSION_DENIED,
                "workload is not assigned to the requested task",
            )
        except PermissionError:
            await self._abort(context, grpc.StatusCode.PERMISSION_DENIED, "operation denied")
        except WorkflowIdempotencyConflictError:
            await self._abort(
                context,
                grpc.StatusCode.ALREADY_EXISTS,
                "idempotency key conflicts with an existing request",
            )
        except RpcReplayConflictError:
            await self._abort(
                context,
                grpc.StatusCode.ALREADY_EXISTS,
                "RPC message_id conflicts with an earlier request",
            )
        except TaskStateConflictError:
            await self._abort(
                context,
                grpc.StatusCode.ABORTED,
                "task state or lease changed; reload canonical task state",
            )
        except (ContractValidationError, ValueError, TypeError) as exc:
            await self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except Exception:
            LOGGER.exception("internal DevOps workflow control operation failed")
            await self._abort(
                context,
                grpc.StatusCode.INTERNAL,
                "internal workflow control failure",
            )
        raise AssertionError("context.abort must terminate the RPC")

    @staticmethod
    def _modules():
        try:
            from .generated import workflow_pb2, workflow_pb2_grpc
        except ImportError as exc:
            raise RuntimeError(
                "Protobuf stubs are missing; run scripts/generate_devops_proto.py"
            ) from exc
        return workflow_pb2, workflow_pb2_grpc

    @staticmethod
    def _set_timestamp(target: Any, value: datetime | None) -> None:
        if value is None:
            return
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        target.FromDatetime(value.astimezone(timezone.utc))

    @staticmethod
    def _stage_principal(principal: str) -> str | None:
        return principal if principal in STAGE_AGENT_IDS else None

    async def GetRun(self, request: Any, context: Any):
        pb, _ = self._modules()
        identity = await self._identity(
            context,
            method="GetRun",
            requested_tenant=request.tenant_id,
        )
        run = await self._call(
            context,
            self.store.get_run,
            tenant_id=identity.tenant_id,
            run_id=request.run_id,
            principal=self._stage_principal(identity.principal),
        )
        response = pb.GetRunResponse(
            tenant_id=run["tenant_id"],
            run_id=run["run_id"],
            status=run["status"],
            state_version=run["state_version"],
            result_ref=run["result_ref"],
            cancel_epoch=run["cancel_epoch"],
        )
        self._set_timestamp(response.created_at, run["created_at"])
        self._set_timestamp(response.updated_at, run["updated_at"])
        return response

    async def GetTask(self, request: Any, context: Any):
        pb, _ = self._modules()
        identity = await self._identity(
            context,
            method="GetTask",
            requested_tenant=request.tenant_id,
        )
        task = await self._call(
            context,
            self.store.get_task,
            tenant_id=identity.tenant_id,
            task_id=request.task_id,
            principal=self._stage_principal(identity.principal),
        )
        from google.protobuf.json_format import ParseDict

        response = pb.GetTaskResponse(
            tenant_id=task["tenant_id"],
            run_id=task["run_id"],
            task_id=task["task_id"],
            attempt=task["attempt"],
            status=task["status"],
            state_version=task["state_version"],
            lease_epoch=task["lease_epoch"],
            lease_owner=task["lease_owner"] or "",
            cancel_epoch=task["cancel_epoch"],
        )
        self._set_timestamp(response.deadline_at, task["deadline_at"])
        self._set_timestamp(response.lease_until, task["lease_until"])
        ParseDict(task["task"], response.task)
        if task["result"] is not None:
            ParseDict(task["result"], response.result)
        return response

    async def ClaimTask(self, request: Any, context: Any):
        pb, _ = self._modules()
        identity = await self._identity(
            context,
            method="ClaimTask",
            requested_tenant=request.tenant_id,
        )
        if identity.principal not in STAGE_AGENT_IDS:
            await self._abort(
                context,
                grpc.StatusCode.PERMISSION_DENIED,
                "only a registered lifecycle stage agent can claim a task",
            )
        claimed = await self._call(
            context,
            self.store.claim_task,
            tenant_id=identity.tenant_id,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            worker_id=identity.principal,
            claim_id=request.claim_id,
            traceparent=request.traceparent,
        )
        response = pb.ClaimTaskResponse(
            accepted=claimed["accepted"],
            lease_epoch=claimed["lease_epoch"],
            state_version=claimed["state_version"],
            lease_owner=claimed["lease_owner"],
            status=claimed["status"],
            cancel_epoch=claimed["cancel_epoch"],
        )
        self._set_timestamp(response.lease_until, claimed["lease_until"])
        self._set_timestamp(response.deadline_at, claimed["deadline_at"])
        return response

    async def HeartbeatTask(self, request: Any, context: Any):
        pb, _ = self._modules()
        identity = await self._identity(
            context,
            method="HeartbeatTask",
            requested_tenant=request.tenant_id,
        )
        if identity.principal not in STAGE_AGENT_IDS:
            await self._abort(
                context,
                grpc.StatusCode.PERMISSION_DENIED,
                "only a registered lifecycle stage agent can renew a lease",
            )
        heartbeat = await self._call(
            context,
            self.store.heartbeat_task,
            tenant_id=identity.tenant_id,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            lease_epoch=request.lease_epoch,
            worker_id=identity.principal,
            claim_id=request.claim_id,
            traceparent=request.traceparent,
        )
        response = pb.HeartbeatTaskResponse(
            accepted=heartbeat["accepted"],
            state_version=heartbeat["state_version"],
            cancel_epoch=heartbeat["cancel_epoch"],
            run_status=heartbeat["run_status"],
            task_status=heartbeat["task_status"],
        )
        self._set_timestamp(response.lease_until, heartbeat["lease_until"])
        return response

    async def ReportTaskProgress(self, request: Any, context: Any):
        pb, _ = self._modules()
        identity = await self._identity(
            context,
            method="ReportTaskProgress",
            requested_tenant=request.tenant_id,
        )
        if identity.principal not in STAGE_AGENT_IDS:
            await self._abort(
                context,
                grpc.StatusCode.PERMISSION_DENIED,
                "only a registered lifecycle stage agent can report progress",
            )
        report = await self._call(
            context,
            self.store.report_task_progress,
            tenant_id=identity.tenant_id,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            lease_epoch=request.lease_epoch,
            expected_state_version=request.expected_state_version,
            percent=request.percent,
            stage=request.stage,
            message_id=request.message_id,
            traceparent=request.traceparent,
            worker_id=identity.principal,
            claim_id=request.claim_id,
        )
        return pb.ReportTaskProgressResponse(
            accepted=report["accepted"],
            state_version=report["state_version"],
        )

    async def ReportTaskResult(self, request: Any, context: Any):
        pb, _ = self._modules()
        identity = await self._identity(
            context,
            method="ReportTaskResult",
            requested_tenant=request.tenant_id,
        )
        if identity.principal not in STAGE_AGENT_IDS:
            await self._abort(
                context,
                grpc.StatusCode.PERMISSION_DENIED,
                "only a registered lifecycle stage agent can report a result",
            )
        from google.protobuf.json_format import MessageToDict

        result = MessageToDict(request.result, preserving_proto_field_name=True)
        report = await self._call(
            context,
            self.store.report_task_result,
            tenant_id=identity.tenant_id,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            lease_epoch=request.lease_epoch,
            expected_state_version=request.expected_state_version,
            message_id=request.message_id,
            traceparent=request.traceparent,
            result=result,
            failure_category=request.failure_category,
            side_effect=request.side_effect,
            diagnostic_ref=request.diagnostic_ref,
            worker_id=identity.principal,
            claim_id=request.claim_id,
        )
        return pb.ReportTaskResultResponse(
            accepted=report["accepted"],
            state_version=report["state_version"],
            status=report["status"],
        )

    async def CancelRun(self, request: Any, context: Any):
        pb, _ = self._modules()
        identity = await self._identity(
            context,
            method="CancelRun",
            requested_tenant=request.tenant_id,
        )
        cancelled = await self._call(
            context,
            self.store.cancel_run,
            tenant_id=identity.tenant_id,
            run_id=request.run_id,
            idempotency_key=request.idempotency_key,
            reason_code=request.reason_code,
            requested_by=identity.principal,
            traceparent=request.traceparent,
        )
        return pb.CancelRunResponse(
            accepted=cancelled["accepted"],
            status=cancelled["status"],
            state_version=cancelled["state_version"],
            cancel_epoch=cancelled["cancel_epoch"],
        )


def create_mtls_server(
    *,
    servicer: WorkflowControlServicer,
    private_key: bytes,
    certificate_chain: bytes,
    client_ca: bytes,
    max_message_bytes: int = 1_048_576,
):
    """Build an async gRPC server that requires client certificate verification."""
    if not private_key or not certificate_chain or not client_ca:
        raise ValueError("gRPC server key, certificate chain, and client CA are required")
    if (
        isinstance(max_message_bytes, bool)
        or not 1024 <= max_message_bytes <= 16_777_216
    ):
        raise ValueError("max_message_bytes must be between 1 KiB and 16 MiB")
    pb, pb_grpc = servicer._modules()
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", max_message_bytes),
            ("grpc.max_send_message_length", max_message_bytes),
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 5_000),
            ("grpc.http2.max_pings_without_data", 2),
        ]
    )
    credentials = grpc.ssl_server_credentials(
        ((private_key, certificate_chain),),
        root_certificates=client_ca,
        require_client_auth=True,
    )
    pb_grpc.add_WorkflowControlServicer_to_server(servicer, server)
    return server, credentials


async def create_mtls_channel(
    *,
    target: str,
    root_ca: bytes,
    certificate_chain: bytes,
    private_key: bytes,
    max_message_bytes: int = 1_048_576,
):
    """Create a workload-mTLS client channel and generated WorkflowControl stub."""
    if not target or not root_ca or not certificate_chain or not private_key:
        raise ValueError("gRPC target and all workload mTLS materials are required")
    if (
        isinstance(max_message_bytes, bool)
        or not 1024 <= max_message_bytes <= 16_777_216
    ):
        raise ValueError("max_message_bytes must be between 1 KiB and 16 MiB")
    _, pb_grpc = WorkflowControlServicer._modules()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root_ca,
        private_key=private_key,
        certificate_chain=certificate_chain,
    )
    channel = grpc.aio.secure_channel(
        target,
        credentials,
        options=[
            ("grpc.max_receive_message_length", max_message_bytes),
            ("grpc.max_send_message_length", max_message_bytes),
        ],
    )
    return channel, pb_grpc.WorkflowControlStub(channel)


async def _serve_from_environment() -> None:
    def read_env_file(name: str) -> bytes:
        path = os.getenv(name)
        if not path:
            raise RuntimeError(f"{name} must point to a mounted PEM file")
        return Path(path).read_bytes()

    profile = os.getenv("RUSA_GRPC_PROFILE", "combined").strip().lower()
    if profile == "orchestrator":
        principal_methods = {
            principal: methods
            for principal, methods in DEFAULT_PRINCIPAL_METHODS.items()
            if principal not in STAGE_AGENT_IDS
        }
    elif profile == "workers":
        principal_methods = {
            principal: DEFAULT_PRINCIPAL_METHODS[principal]
            for principal in STAGE_AGENT_IDS
        }
    elif profile == "combined":
        principal_methods = DEFAULT_PRINCIPAL_METHODS
    else:
        raise RuntimeError("RUSA_GRPC_PROFILE must be orchestrator, workers, or combined")

    api_dsn = os.getenv("RUSA_API_DATABASE_URL") or None
    worker_dsn = os.getenv("RUSA_WORKER_DATABASE_URL") or None
    if profile == "orchestrator" and not api_dsn:
        raise RuntimeError("orchestrator gRPC profile requires RUSA_API_DATABASE_URL")
    if profile == "workers" and not worker_dsn:
        raise RuntimeError("worker gRPC profile requires RUSA_WORKER_DATABASE_URL")
    if profile == "combined" and (not api_dsn or not worker_dsn):
        raise RuntimeError("combined gRPC profile requires both scoped database DSNs")
    store = PostgresWorkflowControlStore(
        api_dsn=api_dsn,
        worker_dsn=worker_dsn,
        lease_seconds=int(os.getenv("RUSA_TASK_LEASE_SECONDS", "30")),
        statement_timeout_ms=int(os.getenv("RUSA_GRPC_STATEMENT_TIMEOUT_MS", "2000")),
    )
    resolver = SpiffeIdentityResolver(
        trust_domain=os.getenv("RUSA_GRPC_TRUST_DOMAIN", "rusa.ai"),
        principal_methods=principal_methods,
    )
    servicer = WorkflowControlServicer(
        store=store,
        identity_resolver=resolver,
        rpc_timeout_seconds=float(os.getenv("RUSA_GRPC_RPC_TIMEOUT_SECONDS", "2")),
    )
    server, credentials = create_mtls_server(
        servicer=servicer,
        private_key=read_env_file("RUSA_GRPC_SERVER_KEY_FILE"),
        certificate_chain=read_env_file("RUSA_GRPC_SERVER_CERT_FILE"),
        client_ca=read_env_file("RUSA_GRPC_CLIENT_CA_FILE"),
    )
    bind_address = os.getenv("RUSA_GRPC_BIND_ADDRESS", "0.0.0.0:7443")
    if server.add_secure_port(bind_address, credentials) == 0:
        raise RuntimeError("gRPC failed to bind the configured address")
    await server.start()
    LOGGER.info("RUSA DevOps WorkflowControl gRPC server listening on %s", bind_address)
    await server.wait_for_termination()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("RUSA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_serve_from_environment())


if __name__ == "__main__":
    main()
