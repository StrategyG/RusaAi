import os  # noqa: I001
import secrets
import uuid

import pytest


DSN = os.getenv("RUSA_TEST_DATABASE_URL")
if not DSN:
    pytest.skip("RUSA_TEST_DATABASE_URL is not configured", allow_module_level=True)


@pytest.fixture(scope="module")
def knowledge_store():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    migration = open("migrations/007_rag_knowledge_pgvector.sql", encoding="utf-8").read()
    readonly_migration = open("migrations/008_rag_mcp_readonly_role.sql", encoding="utf-8").read()
    role_name = "rusa_knowledge_test_" + uuid.uuid4().hex[:12]
    role_password = secrets.token_urlsafe(32)

    with psycopg.connect(DSN, autocommit=True) as admin:
        # The test database is dedicated to CI. The migration is idempotent and must
        # never drop existing application tables or data.
        admin.execute(migration)
        admin.execute(readonly_migration)
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(role_name),
                sql.Literal(role_password),
            )
        )
        admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role_name))
        )
        admin.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON "
                "public.rusa_knowledge_documents, public.rusa_knowledge_chunks TO {}"
            ).format(sql.Identifier(role_name))
        )

    connection_options = conninfo_to_dict(DSN)
    connection_options["user"] = role_name
    connection_options["password"] = role_password
    restricted_dsn = make_conninfo(**connection_options)

    from rusa_knowledge.store import PostgresKnowledgeStore

    store = PostgresKnowledgeStore(
        restricted_dsn,
        min_pool_size=1,
        max_pool_size=2,
        db_role=role_name,
    )
    try:
        assert store.check_ready() is True
        yield store, restricted_dsn
    finally:
        store.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(
                    sql.Identifier(role_name)
                )
            )
            admin.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(
                    sql.Identifier(role_name)
                )
            )
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))


def _unit_vector(index: int) -> list[float]:
    vector = [0.0] * 1536
    vector[index] = 1.0
    return vector


def test_pgvector_hybrid_retrieval_and_tenant_rls(knowledge_store):
    import psycopg

    store, restricted_dsn = knowledge_store
    tenant_a_document = store.ingest_document(
        tenant_id="tenant-a",
        collection="agent",
        source_uri="kb://tenant-a/guide",
        title="Tenant A Guide",
        content_hash="a" * 64,
        metadata={"owner": "tenant-a"},
        chunks=["alpha needle is present in the RUSA knowledge corpus"],
        embeddings=[_unit_vector(0)],
    )
    tenant_b_document = store.ingest_document(
        tenant_id="tenant-b",
        collection="agent",
        source_uri="kb://tenant-b/guide",
        title="Tenant B Guide",
        content_hash="b" * 64,
        metadata={"owner": "tenant-b"},
        chunks=["beta private material is not visible to tenant A"],
        embeddings=[_unit_vector(1)],
    )

    results = store.search(
        tenant_id="tenant-a",
        collection="agent",
        query="needle",
        query_embedding=_unit_vector(0),
        limit=5,
    )
    assert results
    assert results[0]["document_id"] == tenant_a_document
    assert tenant_b_document not in {item["document_id"] for item in results}
    assert results[0]["source_uri"] == "kb://tenant-a/guide"

    document = store.get_document(
        tenant_id="tenant-a",
        collection="agent",
        document_id=tenant_a_document,
    )
    assert document is not None
    assert "alpha needle" in document["content"]
    assert store.list_collections(tenant_id="tenant-a") == [
        {"collection": "agent", "document_count": 1}
    ]

    # Query without a tenant filter: FORCE ROW LEVEL SECURITY must still hide
    # tenant B from the restricted non-superuser application role.
    with psycopg.connect(restricted_dsn) as connection:
        connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", ("tenant-a",))
        visible = connection.execute(
            "SELECT tenant_id FROM public.rusa_knowledge_documents ORDER BY tenant_id"
        ).fetchall()
    assert visible == [("tenant-a",)]



def test_knowledge_store_readiness_rejects_bypassrls_database_identity():
    from rusa_knowledge.store import PostgresKnowledgeStore

    store = PostgresKnowledgeStore(DSN, min_pool_size=1, max_pool_size=1)
    try:
        # CI's database owner is a superuser. It must not be accepted as an
        # application identity for tenant-isolated knowledge operations.
        assert store.check_ready() is False
    finally:
        store.close()


def test_mcp_database_role_is_read_only_and_nobypassrls():
    import psycopg

    with psycopg.connect(DSN) as connection:
        row = connection.execute(
            """
            SELECT
                has_table_privilege(
                    'rusa_knowledge_mcp_reader',
                    'public.rusa_knowledge_documents', 'SELECT'
                ),
                has_table_privilege(
                    'rusa_knowledge_mcp_reader',
                    'public.rusa_knowledge_documents', 'INSERT'
                ),
                has_table_privilege(
                    'rusa_knowledge_mcp_reader',
                    'public.rusa_knowledge_documents', 'UPDATE'
                ),
                has_table_privilege(
                    'rusa_knowledge_mcp_reader',
                    'public.rusa_knowledge_chunks', 'DELETE'
                ),
                (SELECT rolsuper OR rolbypassrls OR rolinherit OR rolcanlogin
                     OR rolcreatedb OR rolcreaterole OR rolreplication
                 FROM pg_roles WHERE rolname='rusa_knowledge_mcp_reader')
            """
        ).fetchone()
    assert row == (True, False, False, False, False)


def test_mcp_reader_login_is_read_only_and_tenant_scoped():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    login_role = "rusa_mcp_test_" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(login_role), sql.Literal(password)
            )
        )
        admin.execute(
            sql.SQL(
                "GRANT rusa_knowledge_mcp_reader TO {} WITH INHERIT FALSE, SET TRUE"
            ).format(sql.Identifier(login_role))
        )
        admin.execute(
            """
            INSERT INTO public.rusa_knowledge_documents
                (tenant_id, collection, source_uri, title, content_hash)
            VALUES
                ('mcp-tenant-a', 'agent', 'kb://mcp/a', 'A', repeat('a', 64)),
                ('mcp-tenant-b', 'agent', 'kb://mcp/b', 'B', repeat('b', 64))
            ON CONFLICT (tenant_id, collection, source_uri) DO NOTHING
            """
        )

    options = conninfo_to_dict(DSN)
    options["user"] = login_role
    options["password"] = password
    reader_dsn = make_conninfo(**options)
    from rusa_knowledge.store import PostgresKnowledgeStore

    store = PostgresKnowledgeStore(
        reader_dsn, min_pool_size=1, max_pool_size=1,
        db_role="rusa_knowledge_mcp_reader",
    )
    try:
        assert store.check_ready() is True
        assert store.check_read_only() is True
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT rusa_knowledge_worker TO {}").format(
                    sql.Identifier(login_role)
                )
            )
        assert store.check_read_only() is False
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("REVOKE rusa_knowledge_worker FROM {}").format(
                    sql.Identifier(login_role)
                )
            )
        assert store.check_read_only() is True
        with psycopg.connect(reader_dsn) as reader:
            reader.execute(
                "SELECT set_config('role', %s, true)",
                ("rusa_knowledge_mcp_reader",),
            )
            reader.execute(
                "SELECT set_config('rusa.tenant_id', %s, true)",
                ("mcp-tenant-a",),
            )
            rows = reader.execute(
                "SELECT tenant_id FROM public.rusa_knowledge_documents ORDER BY tenant_id"
            ).fetchall()
            assert rows == [("mcp-tenant-a",)]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute(
                    """
                    INSERT INTO public.rusa_knowledge_documents
                        (tenant_id, collection, source_uri, title, content_hash)
                    VALUES ('mcp-tenant-a', 'agent', 'kb://mcp/write', 'write', repeat('c', 64))
                    """
                )
    finally:
        store.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("REVOKE rusa_knowledge_mcp_reader FROM {}").format(
                    sql.Identifier(login_role)
                )
            )
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login_role)))
            admin.execute(
                "DELETE FROM public.rusa_knowledge_documents WHERE source_uri IN ('kb://mcp/a', 'kb://mcp/b')"
            )


def test_knowledge_store_readiness_rejects_additional_broad_rls_policy(knowledge_store):
    import psycopg

    store, _ = knowledge_store
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            """
            CREATE POLICY rusa_knowledge_documents_test_bypass
            ON public.rusa_knowledge_documents
            USING (true)
            WITH CHECK (true)
            """
        )
    try:
        assert store.check_ready() is False
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                "DROP POLICY IF EXISTS rusa_knowledge_documents_test_bypass "
                "ON public.rusa_knowledge_documents"
            )


def test_mcp_reader_rejects_superuser_login_even_after_set_role():
    from rusa_knowledge.store import PostgresKnowledgeStore

    # CI's DSN is a superuser. SET ROLE must not hide that privileged session identity.
    store = PostgresKnowledgeStore(
        DSN, min_pool_size=1, max_pool_size=1,
        db_role="rusa_knowledge_mcp_reader",
    )
    try:
        assert store.check_read_only() is False
    finally:
        store.close()
