import asyncio
from threading import Event
from types import SimpleNamespace

from rusa_agents.runtime import AICodeRuntime


def test_worker_does_not_claim_jobs_when_aicode_is_disabled():
    runtime = object.__new__(AICodeRuntime)
    runtime.config = type("Config", (), {"aicode_enabled": False})()

    assert asyncio.run(runtime.run_once()) is False



def test_worker_retries_heartbeat_after_transient_row_lock_timeout():
    renewed = Event()

    class LockTimeout(Exception):
        sqlstate = "55P03"

    class Queue:
        def __init__(self):
            self.calls = 0

        def heartbeat(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise LockTimeout("queue row is temporarily locked")
            renewed.set()
            return True

    runtime = object.__new__(AICodeRuntime)
    runtime.queue = Queue()
    job = SimpleNamespace(
        id=1,
        tenant_id="tenant-a",
        lease_token="00000000-0000-4000-8000-000000000001",
    )
    runtime.worker_id = "heartbeat-test-worker"
    lost_lease = asyncio.Event()

    async def exercise():
        task = asyncio.create_task(
            runtime._heartbeat(job, lost_lease, interval_seconds=0.001)
        )
        try:
            await asyncio.wait_for(asyncio.to_thread(renewed.wait, 1), timeout=2)
            assert not lost_lease.is_set()
            assert runtime.queue.calls >= 2
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(exercise())
