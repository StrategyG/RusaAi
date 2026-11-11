import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from rusa_agents.inference_admission import (
    InferenceDeadlineTimeout,
    InferenceQueueFull,
)
from rusa_agents.postgres_inference_admission import PostgresInferenceAdmissionController

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
