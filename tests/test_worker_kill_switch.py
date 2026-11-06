import asyncio

from rusa_agents.runtime import AICodeRuntime


def test_worker_does_not_claim_jobs_when_aicode_is_disabled():
    runtime = object.__new__(AICodeRuntime)
    runtime.config = type("Config", (), {"aicode_enabled": False})()

    assert asyncio.run(runtime.run_once()) is False
