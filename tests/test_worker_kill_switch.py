import asyncio

from rusa_agents.runtime import AICodeRuntime


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
