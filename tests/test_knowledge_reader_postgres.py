import os
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
    login = "rusa_reader_test_" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    tenant = "tenant-reader-test"

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(migration_007)
        admin.execute(migration_008)
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(login), sql.Literal(password)
            )
        )
        admin.execute(
            sql.SQL("GRANT rusa_knowledge_reader TO {} WITH INHERIT FALSE").format(
                sql.Identifier(login)
            )
        )
        admin.execute(
            """
            INSERT INTO public.rusa_knowledge_documents
                (tenant_id, collection, source_uri, title, content_hash, metadata)
            VALUES
                (%s, 'agent', 'kb://reader-test/a', 'Reader tenant A', %s, '{}'::jsonb),
                ('tenant-other-test', 'agent', 'kb://reader-test/b', 'Other tenant', %s, '{}'::jsonb)
            ON CONFLICT (tenant_id, collection, source_uri) DO NOTHING
            """,
            (tenant, "a" * 64, "b" * 64),
        )

    options = conninfo_to_dict(DSN)
    options["user"] = login
    options["password"] = password
    restricted_dsn = make_conninfo(**options)

    yield restricted_dsn, tenant, login

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            "DELETE FROM public.rusa_knowledge_documents WHERE tenant_id IN (%s, %s)",
            (tenant, "tenant-other-test"),
        )
        admin.execute(
            sql.SQL("REVOKE rusa_knowledge_reader FROM {}").format(sql.Identifier(login))
        )
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


def test_read_only_reader_role_is_tenant_scoped_and_cannot_mutate(reader_database_identity):
    import psycopg

    restricted_dsn, tenant, _login = reader_database_identity
    with psycopg.connect(restricted_dsn) as connection:
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
