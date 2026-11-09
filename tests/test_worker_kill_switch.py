import asyncio

import pytest

from rusa_agents.queue import Job
from rusa_agents.runtime import AICodeRuntime, TaskCancelledError


def test_worker_does_not_claim_jobs_when_aicode_is_disabled():
    runtime = object.__new__(AICodeRuntime)
    runtime.config = type("Config", (), {"aicode_enabled": False})()

    assert asyncio.run(runtime.run_once()) is False


def test_worker_lease_timing_defaults_are_bounded(monkeypatch):
    monkeypatch.delenv("RUSA_TASK_LEASE_SECONDS", raising=False)
    monkeypatch.delenv("RUSA_TASK_HEARTBEAT_SECONDS", raising=False)
    assert AICodeRuntime._bounded_env_int("RUSA_TASK_LEASE_SECONDS", 30, 15, 300) == 30
    assert AICodeRuntime._bounded_env_int("RUSA_TASK_HEARTBEAT_SECONDS", 10, 3, 100) == 10


def test_worker_lease_timing_rejects_out_of_range_values(monkeypatch):
    monkeypatch.setenv("RUSA_TASK_LEASE_SECONDS", "5")
    try:
        AICodeRuntime._bounded_env_int("RUSA_TASK_LEASE_SECONDS", 30, 15, 300)
    except ValueError as exc:
        assert "RUSA_TASK_LEASE_SECONDS" in str(exc)
    else:
        raise AssertionError("invalid lease duration was accepted")



def test_worker_heartbeat_distinguishes_cancellation_from_lease_loss():
    class Queue:
        def heartbeat_state(self, *args):
            return "cancel_requested"

    runtime = object.__new__(AICodeRuntime)
    runtime.heartbeat_interval_seconds = 0
    runtime.lease_seconds = 30
    runtime.queue = Queue()
    runtime.worker_id = "cancel-worker"
    job = Job(1, "task-1", "tenant-a", {}, 1, 3, "cancel-worker", "lease-token")
    lost_lease = asyncio.Event()
    cancel_requested = asyncio.Event()

    asyncio.run(runtime._heartbeat(job, lost_lease, cancel_requested))

    assert cancel_requested.is_set()
    assert not lost_lease.is_set()


def test_worker_cancels_inflight_operation_when_control_plane_requests_cancel():
    runtime = object.__new__(AICodeRuntime)
    lost_lease = asyncio.Event()
    cancel_requested = asyncio.Event()

    async def operation():
        await asyncio.Event().wait()

    async def request_cancel():
        await asyncio.sleep(0.01)
        cancel_requested.set()

    async def run():
        asyncio.create_task(request_cancel())
        with pytest.raises(TaskCancelledError):
            await runtime._await_with_lease(operation(), lost_lease, cancel_requested)

    asyncio.run(run())
