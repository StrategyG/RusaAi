from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import grpc
import pytest
from google.protobuf.json_format import ParseDict

from rusa_agents.devops.control_store import WorkflowNotFoundError
from rusa_agents.devops.generated import workflow_pb2 as pb
from rusa_agents.devops.grpc_server import (
    DEFAULT_PRINCIPAL_METHODS,
    IdentityAuthenticationError,
    SpiffeIdentityResolver,
    WorkflowControlServicer,
)


def _spiffe(principal="rusa_code_agent", tenant="tenant-a", domain="rusa.ai"):
    return f"spiffe://{domain}/tenant/{tenant}/service/{principal}"


def _auth_context(uri=None, *, security=b"ssl", extra_sans=()):
    sans = []
    if uri is not None:
        sans.append(uri.encode())
    sans.extend(value.encode() for value in extra_sans)
    return {
        "transport_security_type": [security],
        "x509_subject_alternative_name": sans,
    }


class FakeAbort(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details
        super().__init__(details)


class FakeContext:
    def __init__(self, uri=None, *, tenant="tenant-a", remaining=1.0, security=b"ssl"):
        self._auth = _auth_context(uri or _spiffe(tenant=tenant), security=security)
        self.remaining = remaining

    def auth_context(self):
        return self._auth

    def time_remaining(self):
        return self.remaining

    async def abort(self, code, details):
        raise FakeAbort(code, details)


class FakeStore:
    def __init__(self):
        self.calls = []
        self.run = {
            "tenant_id": "tenant-a",
            "run_id": "run-1",
            "status": "running",
            "state_version": 3,
            "result_ref": "",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "cancel_epoch": 0,
        }
        self.task = {
            "tenant_id": "tenant-a",
            "run_id": "run-1",
            "task_id": "task-1",
            "agent_id": "rusa_code_agent",
            "stage": "code",
            "attempt": 1,
            "status": "running",
            "state_version": 4,
            "lease_epoch": 2,
            "deadline_at": datetime.now(timezone.utc),
            "task": {"schema_version": 1, "task_id": "task-1"},
            "result": None,
            "lease_owner": "rusa_code_agent",
            "lease_until": datetime.now(timezone.utc),
            "cancel_epoch": 0,
        }

    def get_run(self, **kwargs):
        self.calls.append(("get_run", kwargs))
        return self.run

    def get_task(self, **kwargs):
        self.calls.append(("get_task", kwargs))
        return self.task

    def claim_task(self, **kwargs):
        self.calls.append(("claim_task", kwargs))
        return {
            "accepted": True,
            "lease_epoch": 2,
            "state_version": 4,
            "lease_owner": kwargs["worker_id"],
            "lease_until": datetime.now(timezone.utc),
            "deadline_at": datetime.now(timezone.utc),
            "status": "accepted",
            "cancel_epoch": 0,
        }

    def heartbeat_task(self, **kwargs):
        self.calls.append(("heartbeat_task", kwargs))
        return {
            "accepted": True,
            "state_version": 4,
            "lease_until": datetime.now(timezone.utc),
            "cancel_epoch": 0,
            "run_status": "running",
            "task_status": "running",
        }

    def report_task_progress(self, **kwargs):
        self.calls.append(("report_task_progress", kwargs))
        return {"accepted": True, "state_version": 5}

    def report_task_result(self, **kwargs):
        self.calls.append(("report_task_result", kwargs))
        return {"accepted": True, "state_version": 6, "status": "completed"}

    def cancel_run(self, **kwargs):
        self.calls.append(("cancel_run", kwargs))
        return {
            "accepted": True,
            "status": "cancel_requested",
            "state_version": 4,
            "cancel_epoch": 1,
        }


def _servicer(store=None, *, timeout=2.0, resolver=None):
    return WorkflowControlServicer(
        store=store or FakeStore(),
        identity_resolver=resolver or SpiffeIdentityResolver(trust_domain="rusa.ai"),
        rpc_timeout_seconds=timeout,
    )


def test_spiffe_identity_binds_principal_and_tenant_from_mtls_certificate():
    resolver = SpiffeIdentityResolver(trust_domain="rusa.ai")
    identity = resolver.resolve(_auth_context(_spiffe()))
    assert identity.tenant_id == "tenant-a"
    assert identity.principal == "rusa_code_agent"
    assert "ClaimTask" in identity.allowed_methods
    assert identity.spiffe_id == _spiffe()


@pytest.mark.parametrize(
    "auth_context",
    [
        _auth_context(_spiffe(), security=b"insecure"),
        _auth_context("spiffe://evil.example/tenant/tenant-a/service/rusa_code_agent"),
        _auth_context("spiffe://rusa.ai/tenant/tenant-a/service/unknown_agent"),
        _auth_context("spiffe://rusa.ai/service/rusa_code_agent"),
        _auth_context(_spiffe(), extra_sans=("spiffe://rusa.ai/tenant/tenant-a/service/rusa_test_agent",)),
    ],
)
def test_spiffe_resolver_rejects_untrusted_or_ambiguous_identity(auth_context):
    with pytest.raises(IdentityAuthenticationError):
        SpiffeIdentityResolver(trust_domain="rusa.ai").resolve(auth_context)


def test_all_ten_system_agents_have_read_only_or_orchestrator_scopes():
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_system_architect_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_planner_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_task_decomposer_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_software_engineer_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_devops_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_qa_testing_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_security_policy_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_memory_knowledge_agent"]
    assert "GetRun" in DEFAULT_PRINCIPAL_METHODS["rusa_evaluation_reporting_agent"]
    assert "CancelRun" in DEFAULT_PRINCIPAL_METHODS["rusa_supreme_orchestrator"]
    assert "CancelRun" not in DEFAULT_PRINCIPAL_METHODS["rusa_code_agent"]


@pytest.mark.asyncio
async def test_get_run_returns_canonical_tenant_bound_workflow():
    store = FakeStore()
    service = _servicer(store)
    response = await service.GetRun(
        pb.GetRunRequest(tenant_id="tenant-a", run_id="run-1"),
        FakeContext(_spiffe("rusa_supreme_orchestrator")),
    )
    assert response.run_id == "run-1"
    assert response.status == "running"
    assert response.state_version == 3
    assert store.calls[0][1]["principal"] is None


@pytest.mark.asyncio
async def test_get_run_rejects_tenant_header_override():
    service = _servicer()
    with pytest.raises(FakeAbort) as error:
        await service.GetRun(
            pb.GetRunRequest(tenant_id="tenant-b", run_id="run-1"),
            FakeContext(_spiffe("rusa_supreme_orchestrator")),
        )
    assert error.value.code == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_get_task_limits_stage_agent_to_its_assigned_task():
    store = FakeStore()
    service = _servicer(store)
    response = await service.GetTask(
        pb.GetTaskRequest(tenant_id="tenant-a", task_id="task-1"),
        FakeContext(_spiffe("rusa_code_agent")),
    )
    assert response.task_id == "task-1"
    assert response.lease_epoch == 2
    assert store.calls[0][1]["principal"] == "rusa_code_agent"


@pytest.mark.asyncio
async def test_claim_heartbeat_and_progress_are_bound_to_certificate_principal():
    store = FakeStore()
    service = _servicer(store)
    context = FakeContext(_spiffe("rusa_code_agent"))
    claim = await service.ClaimTask(
        pb.ClaimTaskRequest(
            tenant_id="tenant-a",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ),
        context,
    )
    heartbeat = await service.HeartbeatTask(
        pb.HeartbeatTaskRequest(
            tenant_id="tenant-a",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            lease_epoch=2,
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ),
        context,
    )
    progress = await service.ReportTaskProgress(
        pb.ReportTaskProgressRequest(
            tenant_id="tenant-a",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            lease_epoch=2,
            expected_state_version=4,
            percent=50,
            stage="code",
            message_id="progress-1",
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ),
        context,
    )
    assert claim.accepted is True
    assert heartbeat.accepted is True
    assert progress.accepted is True
    assert [call[1]["worker_id"] for call in store.calls] == [
        "rusa_code_agent",
        "rusa_code_agent",
        "rusa_code_agent",
    ]


@pytest.mark.asyncio
async def test_orchestrator_cannot_claim_or_report_worker_task():
    service = _servicer()
    with pytest.raises(FakeAbort) as error:
        await service.ClaimTask(
            pb.ClaimTaskRequest(
                tenant_id="tenant-a",
                run_id="run-1",
                task_id="task-1",
                attempt=1,
                traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            ),
            FakeContext(_spiffe("rusa_devops_orchestrator")),
        )
    assert error.value.code == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_report_result_converts_protobuf_struct_and_passes_failure_metadata():
    store = FakeStore()
    service = _servicer(store)
    result = {
        "schema_version": 1,
        "task_id": "task-1",
        "workflow_id": "run-1",
        "tenant_id": "tenant-a",
        "agent_id": "rusa_code_agent",
        "stage": "code",
        "attempt": 1,
        "status": "failed",
        "output": {
            "summary": "The change did not pass.",
            "artifact_refs": [],
            "evidence_refs": [],
            "findings": [{"severity": "high", "code": "test_failed", "message": "A test failed"}],
        },
        "audit": {
            "started_at": "2026-11-08T00:00:00Z",
            "completed_at": "2026-11-08T00:01:00Z",
            "policy_version": "v1",
        },
    }
    message = pb.ReportTaskResultRequest(
        tenant_id="tenant-a",
        run_id="run-1",
        task_id="task-1",
        attempt=1,
        lease_epoch=2,
        expected_state_version=4,
        message_id="result-1",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        failure_category="PERMANENT",
        side_effect="NONE",
        diagnostic_ref="artifact://diagnostics/1",
    )
    ParseDict(result, message.result)
    response = await service.ReportTaskResult(
        message,
        FakeContext(_spiffe("rusa_code_agent")),
    )
    assert response.accepted is True
    assert store.calls[0][0] == "report_task_result"
    assert store.calls[0][1]["result"]["task_id"] == "task-1"
    assert store.calls[0][1]["failure_category"] == "PERMANENT"
    assert store.calls[0][1]["side_effect"] == "NONE"


@pytest.mark.asyncio
async def test_cancel_run_is_reserved_for_orchestrator_and_records_authenticated_actor():
    store = FakeStore()
    service = _servicer(store)
    response = await service.CancelRun(
        pb.CancelRunRequest(
            tenant_id="tenant-a",
            run_id="run-1",
            idempotency_key="cancel-run-1",
            reason_code="operator_requested",
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ),
        FakeContext(_spiffe("rusa_supreme_orchestrator")),
    )
    assert response.accepted is True
    assert response.cancel_epoch == 1
    assert store.calls[0][1]["requested_by"] == "rusa_supreme_orchestrator"


@pytest.mark.asyncio
async def test_grpc_requires_absolute_rpc_deadline():
    service = _servicer()
    context = FakeContext(_spiffe("rusa_supreme_orchestrator"), remaining=None)
    with pytest.raises(FakeAbort) as error:
        await service.GetRun(
            pb.GetRunRequest(tenant_id="tenant-a", run_id="run-1"),
            context,
        )
    assert error.value.code == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_grpc_maps_missing_workflow_to_not_found():
    class MissingStore(FakeStore):
        def get_run(self, **_kwargs):
            raise WorkflowNotFoundError("internal detail must not leak")

    service = _servicer(MissingStore())
    with pytest.raises(FakeAbort) as error:
        await service.GetRun(
            pb.GetRunRequest(tenant_id="tenant-a", run_id="missing"),
            FakeContext(_spiffe("rusa_supreme_orchestrator")),
        )
    assert error.value.code == grpc.StatusCode.NOT_FOUND
    assert "internal detail" not in error.value.details
