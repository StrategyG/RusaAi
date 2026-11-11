import asyncio

import pytest

from rusa_agents.inference_admission import (
    InferenceAdmissionController,
    InferenceQueueFull,
    InferenceQueueTimeout,
)


def test_admission_is_fifo_and_waiting_queue_is_bounded():
    async def scenario():
        controller = InferenceAdmissionController(max_active=1, max_waiting=2, queue_timeout=1)
        await controller.acquire()
        order = []

        async def queued_request(name):
            await controller.acquire()
            order.append(name)
            await controller.release()

        first = asyncio.create_task(queued_request("first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(queued_request("second"))
        await asyncio.sleep(0)

        assert controller.active_count == 1
        assert controller.waiting_count == 2
        with pytest.raises(InferenceQueueFull):
            await controller.acquire()

        await controller.release()
        await asyncio.gather(first, second)

        assert order == ["first", "second"]
        assert controller.active_count == 0
        assert controller.waiting_count == 0

    asyncio.run(scenario())


def test_timed_out_request_is_removed_from_waiting_queue():
    async def scenario():
        controller = InferenceAdmissionController(max_active=1, max_waiting=1, queue_timeout=0.1)
        await controller.acquire()

        with pytest.raises(InferenceQueueTimeout):
            await controller.acquire()

        assert controller.active_count == 1
        assert controller.waiting_count == 0
        await controller.release()

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_leak_queue_capacity():
    async def scenario():
        controller = InferenceAdmissionController(max_active=1, max_waiting=1, queue_timeout=1)
        await controller.acquire()
        waiting = asyncio.create_task(controller.acquire())
        await asyncio.sleep(0)

        assert controller.waiting_count == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        assert controller.waiting_count == 0
        assert controller.active_count == 1
        await controller.release()

    asyncio.run(scenario())


def test_cancelled_request_after_slot_handoff_releases_the_slot():
    async def scenario():
        controller = InferenceAdmissionController(max_active=1, max_waiting=1, queue_timeout=1)
        await controller.acquire()
        started = asyncio.Event()

        async def request():
            await controller.acquire()
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await controller.release()

        waiting = asyncio.create_task(request())
        await asyncio.sleep(0)
        await controller.release()
        await started.wait()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        assert controller.active_count == 0
        assert controller.waiting_count == 0

    asyncio.run(scenario())


def test_release_without_acquired_slot_is_rejected():
    async def scenario():
        controller = InferenceAdmissionController()
        with pytest.raises(RuntimeError, match="not held"):
            await controller.release()

    asyncio.run(scenario())


def test_invalid_admission_limits_are_rejected():
    with pytest.raises(ValueError):
        InferenceAdmissionController(max_active=0)
    with pytest.raises(ValueError):
        InferenceAdmissionController(max_waiting=-1)
    with pytest.raises(ValueError):
        InferenceAdmissionController(queue_timeout=0)
