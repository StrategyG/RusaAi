import os  # noqa: I001
import secrets
import uuid

import pytest


DSN = os.getenv("RUSA_TEST_DATABASE_URL")
if not DSN:
    pytest.skip("RUSA_TEST_DATABASE_URL is not configured", allow_module_level=True)


@pytest.fixture(scope="module")
def reader_database_identity():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    migration_007 = open(
        "migrations/007_rag_knowledge_pgvector.sql", encoding="utf-8"
    ).read()
    migration_008 = open(
        "migrations/008_rag_read_only_reader.sql", encoding="utf-8"
    ).read()
    reader_login = "rusa_reader_test_" + uuid.uuid4().hex[:12]
    worker_login = "rusa_worker_reader_test_" + uuid.uuid4().hex[:12]
    reader_password = secrets.token_urlsafe(32)
    worker_password = secrets.token_urlsafe(32)
    tenant = "tenant-reader-" + uuid.uuid4().hex[:10]
    other_tenant = "tenant-other-" + uuid.uuid4().hex[:10]

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            """
            DO $rusa$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'rusa_worker_runtime'
                ) THEN
                    CREATE ROLE rusa_worker_runtime NOLOGIN;
                END IF;
            END;
            $rusa$;
            """
        )
        admin.execute(migration_007)
        admin.execute(migration_008)
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(reader_login), sql.Literal(reader_password)
            )
        )
        admin.execute(
            sql.SQL("GRANT rusa_knowledge_reader TO {} WITH INHERIT FALSE").format(
                sql.Identifier(reader_login)
            )
        )
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} IN ROLE rusa_worker_runtime"
            ).format(sql.Identifier(worker_login), sql.Literal(worker_password))
        )
        admin.execute(
            """
            INSERT INTO public.rusa_knowledge_documents
                (tenant_id, collection, source_uri, title, content_hash, metadata)
            VALUES
                (%s, 'agent', 'kb://reader-test/a', 'Reader tenant A', %s, '{}'::jsonb),
                (%s, 'agent', 'kb://reader-test/b', 'Other tenant', %s, '{}'::jsonb)
            ON CONFLICT (tenant_id, collection, source_uri) DO NOTHING
            """,
            (tenant, "a" * 64, other_tenant, "b" * 64),
        )

    options = conninfo_to_dict(DSN)
    options["user"] = reader_login
    options["password"] = reader_password
    reader_dsn = make_conninfo(**options)
    options["user"] = worker_login
    options["password"] = worker_password
    worker_dsn = make_conninfo(**options)

    yield reader_dsn, worker_dsn, tenant, other_tenant, reader_login, worker_login

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            "DELETE FROM public.rusa_knowledge_documents WHERE tenant_id IN (%s, %s)",
            (tenant, other_tenant),
        )
        admin.execute(
            sql.SQL("REVOKE rusa_knowledge_reader FROM {}").format(
                sql.Identifier(reader_login)
            )
        )
        admin.execute(
            sql.SQL("REVOKE rusa_worker_runtime FROM {}").format(
                sql.Identifier(worker_login)
            )
        )
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(reader_login)))
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(worker_login)))


def test_read_only_reader_role_is_tenant_scoped_and_cannot_mutate(reader_database_identity):
    import psycopg

    from rusa_knowledge.store import PostgresKnowledgeStore

    reader_dsn, worker_dsn, tenant, _other_tenant, _reader_login, _worker_login = (
        reader_database_identity
    )

    for dsn in (reader_dsn, worker_dsn):
        store = PostgresKnowledgeStore(
            dsn, min_pool_size=1, max_pool_size=1,
            db_role="rusa_knowledge_reader",
        )
        try:
            assert store.check_ready() is True
        finally:
            store.close()

    with psycopg.connect(reader_dsn) as connection:
        connection.execute(
            "SELECT set_config('role', 'rusa_knowledge_reader', true)"
        )
        connection.execute(
            "SELECT set_config('rusa.tenant_id', %s, true)", (tenant,)
        )
        visible = connection.execute(
            "SELECT tenant_id, title FROM public.rusa_knowledge_documents "
            "WHERE source_uri LIKE 'kb://reader-test/%' ORDER BY tenant_id"
        ).fetchall()
        assert visible == [(tenant, "Reader tenant A")]

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "DELETE FROM public.rusa_knowledge_documents WHERE tenant_id = %s",
                (tenant,),
            )


def test_reader_migration_grants_select_only():
    from pathlib import Path

    migration = Path(
        "migrations/008_rag_read_only_reader.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE ROLE rusa_knowledge_reader" in migration
    assert "GRANT SELECT" in migration
    assert "REVOKE rusa_knowledge_worker FROM rusa_worker_runtime" in migration
    assert "GRANT rusa_knowledge_reader TO rusa_worker_runtime WITH INHERIT FALSE" in migration
    assert "FOR SELECT TO rusa_knowledge_reader" in migration
