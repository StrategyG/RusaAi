import asyncio
import multiprocessing
import os
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.inference_admission import (
    InferenceDeadlineTimeout,
    InferenceQueueFull,
)
from rusa_agents.postgres_inference_admission import PostgresInferenceAdmissionController


def _hold_scheduler_slot_in_process(dsn, label, release_event, result_queue):
    os.environ["ENVIRONMENT"] = "development"
    controller = PostgresInferenceAdmissionController(
        dsn,
        max_active=1,
        max_waiting=2,
        queue_timeout=10.0,
        lease_seconds=5,
        poll_interval=0.01,
    )
    controller.initialize()

    async def run():
        await controller.acquire()
        request_id = str(next(iter(controller._owner_ids)))
        result_queue.put((label, request_id))
        await asyncio.to_thread(release_event.wait)
        await controller.release()

    asyncio.run(run())


DSN = os.getenv("RUSA_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="PostgreSQL integration DSN is not configured"
)


@pytest.fixture(scope="module", autouse=True)
def apply_inference_admission_migration():
    if not DSN:
        pytest.skip("PostgreSQL integration DSN is not configured")
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "010_inference_admission.sql"
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            """
            DO $rusa$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_api_runtime') THEN
                    CREATE ROLE rusa_api_runtime NOLOGIN NOBYPASSRLS;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rusa_worker_runtime') THEN
                    CREATE ROLE rusa_worker_runtime NOLOGIN NOBYPASSRLS;
                END IF;
            END;
            $rusa$;
            """
        )
        conn.execute(migration.read_text(encoding="utf-8"), prepare=False)


@pytest.fixture
def scheduler_database(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    with psycopg.connect(DSN) as conn:
        conn.execute(
            """
            UPDATE public.rusa_inference_admission_slots
            SET active_request_id=NULL, lease_owner=NULL, lease_expires_at=NULL
            """
        )
        conn.execute("DELETE FROM public.rusa_inference_admission_requests")
        conn.execute(
            """
            UPDATE public.rusa_inference_admission_config
            SET max_active=1, max_waiting=3, queue_timeout_ms=2000, lease_seconds=5
            WHERE singleton_id=1
            """
        )
        conn.execute(
            "DELETE FROM public.rusa_inference_admission_slots WHERE slot_id > 0"
        )
        conn.execute(
            "INSERT INTO public.rusa_inference_admission_slots(slot_id) VALUES (0) ON CONFLICT DO NOTHING"
        )
    return DSN


def make_controller(dsn, *, max_waiting=2, queue_timeout=2.0, lease_seconds=5):
    controller = PostgresInferenceAdmissionController(
        dsn,
        max_active=1,
        max_waiting=max_waiting,
        queue_timeout=queue_timeout,
        lease_seconds=lease_seconds,
        poll_interval=0.01,
    )
    controller.initialize()
    return controller


def test_independent_controllers_share_one_fifo_generation_slot(scheduler_database):
    first_controller = make_controller(scheduler_database)
    second_controller = make_controller(scheduler_database)
    third_controller = make_controller(scheduler_database)

    async def scenario():
        order = []
        await first_controller.acquire()

        async def queued(controller, name):
            await controller.acquire()
            order.append(name)
            await asyncio.sleep(0.03)
            await controller.release()

        first = asyncio.create_task(queued(second_controller, "first"))
        await asyncio.sleep(0.08)
        second = asyncio.create_task(queued(third_controller, "second"))
        await asyncio.sleep(0.08)

        with psycopg.connect(scheduler_database) as conn:
            assert conn.execute(
                "SELECT count(*) FROM rusa_inference_admission_requests WHERE status='waiting'"
            ).fetchone()[0] == 2

        await first_controller.release()
        await asyncio.gather(first, second)

        assert order == ["first", "second"]
        with psycopg.connect(scheduler_database) as conn:
            assert conn.execute(
                "SELECT count(*) FROM rusa_inference_admission_slots WHERE active_request_id IS NOT NULL"
            ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_global_waiting_capacity_and_cancelled_waiter_are_cleaned(scheduler_database):
    first_controller = make_controller(scheduler_database, max_waiting=1)
    second_controller = make_controller(scheduler_database, max_waiting=1)
    third_controller = make_controller(scheduler_database, max_waiting=1)

    async def scenario():
        await first_controller.acquire()
        waiting = asyncio.create_task(second_controller.acquire())
        await asyncio.sleep(0.08)

        with pytest.raises(InferenceQueueFull):
            await third_controller.acquire()

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        with psycopg.connect(scheduler_database) as conn:
            assert conn.execute(
                "SELECT count(*) FROM rusa_inference_admission_requests WHERE status='waiting'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM rusa_inference_admission_requests WHERE status='cancelled'"
            ).fetchone()[0] == 1

        await first_controller.release()

    asyncio.run(scenario())


def test_expired_owner_lease_is_reclaimed_without_releasing_new_owner(scheduler_database):
    first_controller = make_controller(scheduler_database)
    second_controller = make_controller(scheduler_database)

    async def scenario():
        await first_controller.acquire()
        old_request_id = next(iter(first_controller._owner_ids))
        heartbeat = first_controller._heartbeat_tasks.pop(old_request_id)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        with psycopg.connect(scheduler_database) as conn:
            conn.execute(
                """
                UPDATE rusa_inference_admission_slots
                SET lease_expires_at=clock_timestamp() - INTERVAL '1 second'
                WHERE active_request_id=%s
                """,
                (old_request_id,),
            )

        await second_controller.acquire()
        with psycopg.connect(scheduler_database) as conn:
            old_status = conn.execute(
                "SELECT status FROM rusa_inference_admission_requests WHERE request_id=%s",
                (old_request_id,),
            ).fetchone()[0]
            new_active = conn.execute(
                "SELECT active_request_id FROM rusa_inference_admission_slots WHERE slot_id=0"
            ).fetchone()[0]

        assert old_status == "expired"
        assert new_active != old_request_id

        # A late release from the stale owner must not clear the new owner's slot.
        await first_controller.release()
        with psycopg.connect(scheduler_database) as conn:
            assert conn.execute(
                "SELECT active_request_id FROM rusa_inference_admission_slots WHERE slot_id=0"
            ).fetchone()[0] == new_active

        await second_controller.release()

    asyncio.run(scenario())


def test_production_scheduler_requires_migration_and_matching_cluster_settings(
    scheduler_database, monkeypatch
):
    monkeypatch.setenv("ENVIRONMENT", "production")
    controller = PostgresInferenceAdmissionController(
        scheduler_database,
        max_active=1,
        max_waiting=3,
        queue_timeout=2.0,
        lease_seconds=5,
    )
    controller.initialize()

    mismatch = PostgresInferenceAdmissionController(
        scheduler_database,
        max_active=1,
        max_waiting=2,
        queue_timeout=2.0,
        lease_seconds=5,
    )
    with pytest.raises(RuntimeError, match="do not match"):
        mismatch.initialize()


def test_request_deadline_is_classified_separately_from_queue_timeout(scheduler_database):
    controller = make_controller(scheduler_database, max_waiting=1, queue_timeout=2.0)

    async def scenario():
        await controller.acquire()
        with pytest.raises(InferenceDeadlineTimeout):
            await controller.acquire(timeout_seconds=0.05)
        with psycopg.connect(scheduler_database) as conn:
            assert conn.execute(
                "SELECT count(*) FROM rusa_inference_admission_requests WHERE status='waiting'"
            ).fetchone()[0] == 0
        await controller.release()

    asyncio.run(scenario())



def test_production_api_runtime_can_use_scheduler_but_worker_runtime_cannot(
    scheduler_database, monkeypatch
):
    api_login = "rusa_admission_api_" + uuid4().hex[:8]
    worker_login = "rusa_admission_worker_" + uuid4().hex[:8]
    api_password = "api-" + uuid4().hex
    worker_password = "worker-" + uuid4().hex
    from psycopg import sql

    with psycopg.connect(scheduler_database) as admin:
        for login, password, group in (
            (api_login, api_password, "rusa_api_runtime"),
            (worker_login, worker_password, "rusa_worker_runtime"),
        ):
            admin.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOBYPASSRLS "
                    "NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(login), sql.Literal(password))
            )
            admin.execute(
                sql.SQL("GRANT {} TO {} WITH ADMIN FALSE, INHERIT TRUE, SET TRUE").format(
                    sql.Identifier(group), sql.Identifier(login)
                )
            )

    api_dsn = psycopg.conninfo.make_conninfo(
        scheduler_database, user=api_login, password=api_password
    )
    worker_dsn = psycopg.conninfo.make_conninfo(
        scheduler_database, user=worker_login, password=worker_password
    )
    monkeypatch.setenv("ENVIRONMENT", "production")
    controller = PostgresInferenceAdmissionController(
        api_dsn,
        max_active=1,
        max_waiting=3,
        queue_timeout=2.0,
        lease_seconds=5,
    )
    controller.initialize()

    with psycopg.connect(worker_dsn) as worker:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("SELECT * FROM public.rusa_inference_admission_requests")

    with psycopg.connect(scheduler_database) as admin:
        admin.execute(
            sql.SQL("REVOKE rusa_api_runtime, rusa_worker_runtime FROM {}").format(
                sql.Identifier(api_login)
            )
        )
        admin.execute(
            sql.SQL("REVOKE rusa_api_runtime, rusa_worker_runtime FROM {}").format(
                sql.Identifier(worker_login)
            )
        )
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(api_login)))
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(worker_login)))



def test_fifo_admission_is_shared_across_separate_os_processes(scheduler_database):
    context = multiprocessing.get_context("spawn")
    first_release = context.Event()
    second_release = context.Event()
    result_queue = context.Queue()
    first_process = context.Process(
        target=_hold_scheduler_slot_in_process,
        args=(scheduler_database, "process-one", first_release, result_queue),
    )
    second_process = context.Process(
        target=_hold_scheduler_slot_in_process,
        args=(scheduler_database, "process-two", second_release, result_queue),
    )
    parent_controller = make_controller(
        scheduler_database, max_waiting=2, queue_timeout=10.0
    )

    async def wait_for_waiters(expected):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with psycopg.connect(scheduler_database) as conn:
                count = conn.execute(
                    "SELECT count(*) FROM rusa_inference_admission_requests WHERE status='waiting'"
                ).fetchone()[0]
            if count >= expected:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"Expected {expected} PostgreSQL scheduler waiters")

    async def scenario():
        await parent_controller.acquire()
        first_process.start()
        await wait_for_waiters(1)
        second_process.start()
        await wait_for_waiters(2)
        await parent_controller.release()

        first_label, _ = await asyncio.to_thread(result_queue.get, True, 10)
        assert first_label == "process-one"
        first_release.set()
        second_label, _ = await asyncio.to_thread(result_queue.get, True, 10)
        assert second_label == "process-two"
        second_release.set()

    try:
        asyncio.run(scenario())
        first_process.join(timeout=8)
        second_process.join(timeout=8)
        assert first_process.exitcode == 0
        assert second_process.exitcode == 0
    finally:
        first_release.set()
        second_release.set()
        for process in (first_process, second_process):
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
        result_queue.close()
        result_queue.join_thread()


def test_process_crash_recovers_after_global_lease_expiry(scheduler_database):
    context = multiprocessing.get_context("spawn")
    release_event = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_scheduler_slot_in_process,
        args=(scheduler_database, "crashed-process", release_event, result_queue),
    )
    controller = make_controller(scheduler_database, queue_timeout=10.0)

    async def scenario():
        process.start()
        label, old_request_id = await asyncio.to_thread(result_queue.get, True, 10)
        assert label == "crashed-process"
        process.terminate()
        process.join(timeout=3)
        assert process.exitcode is not None

        # The lease is five seconds; wait beyond its expiry because the process
        # and its heartbeat are now gone, then let a new API process reclaim it.
        await asyncio.sleep(5.5)
        await controller.acquire()
        with psycopg.connect(scheduler_database) as conn:
            old_status = conn.execute(
                "SELECT status FROM rusa_inference_admission_requests WHERE request_id=%s",
                (old_request_id,),
            ).fetchone()[0]
        assert old_status == "expired"
        await controller.release()

    try:
        asyncio.run(scenario())
    finally:
        release_event.set()
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        result_queue.close()
        result_queue.join_thread()


def test_scheduler_database_connections_have_bounded_timeouts(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    controller = PostgresInferenceAdmissionController(
        "postgresql://scheduler.internal/rusa",
        max_active=1,
        max_waiting=1,
        queue_timeout=2.0,
        lease_seconds=5,
    )

    assert controller._connect() is sentinel
    assert captured["dsn"] == "postgresql://scheduler.internal/rusa"
    assert captured["kwargs"]["connect_timeout"] == 5
    assert captured["kwargs"]["application_name"] == "rusa-inference-admission"
    assert "lock_timeout=5000" in captured["kwargs"]["options"]
    assert "statement_timeout=5000" in captured["kwargs"]["options"]
    assert "idle_in_transaction_session_timeout=10000" in captured["kwargs"]["options"]


def test_lease_renewal_failure_cancels_generation_and_releases_slot(
    scheduler_database, monkeypatch
):
    controller = make_controller(scheduler_database)
    monkeypatch.setattr(controller, "_renew", lambda request_id, owner_id: False)

    cancelled = asyncio.Event()

    async def owner_request():
        await controller.acquire()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            await controller.release()

    async def scenario():
        task = asyncio.create_task(owner_request())
        await asyncio.wait_for(cancelled.wait(), timeout=4.0)
        with pytest.raises(asyncio.CancelledError):
            await task

        with psycopg.connect(scheduler_database) as conn:
            assert conn.execute(
                "SELECT count(*) FROM rusa_inference_admission_slots "
                "WHERE active_request_id IS NOT NULL"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM rusa_inference_admission_requests "
                "WHERE status='active'"
            ).fetchone()[0] == 0

    asyncio.run(scenario())
