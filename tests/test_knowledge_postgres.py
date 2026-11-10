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
    embedding_migration = open("migrations/009_knowledge_embedding_384.sql", encoding="utf-8").read()
    role_name = "rusa_knowledge_test_" + uuid.uuid4().hex[:12]
    role_password = secrets.token_urlsafe(32)

    with psycopg.connect(DSN, autocommit=True) as admin:
        # The test database is dedicated to CI. The migration is idempotent and must
        # never drop existing application tables or data.
        admin.execute(migration)
        admin.execute(embedding_migration)
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



def _unit_vector_384(index: int) -> list[float]:
    vector = [0.0] * 384
    vector[index] = 1.0
    return vector


def test_pgvector_384d_backfill_preserves_legacy_vectors(knowledge_store):
    from rusa_knowledge.store import PostgresKnowledgeStore

    legacy_store, restricted_dsn = knowledge_store
    tenant_id = "tenant-reembed"
    document_id = legacy_store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://tenant-reembed/legacy",
        title="Legacy guide",
        content_hash="c" * 64,
        metadata={"owner": tenant_id},
        chunks=["RUSA legacy knowledge needs new E5 embeddings"],
        embeddings=[_unit_vector(0)],
    )

    e5_store = PostgresKnowledgeStore(
        restricted_dsn,
        dimensions=384,
        min_pool_size=1,
        max_pool_size=2,
        db_role=legacy_store.db_role,
    )
    try:
        assert e5_store.check_ready() is True
        batch = e5_store.fetch_chunks_for_reembedding(
            tenant_id=tenant_id,
            collection="agent",
            limit=16,
        )
        assert len(batch) == 1
        assert batch[0]["content"] == "RUSA legacy knowledge needs new E5 embeddings"
        updated = e5_store.update_reembedded_embeddings(
            tenant_id=tenant_id,
            vectors=[(batch[0]["chunk_id"], _unit_vector_384(7))],
        )
        assert updated == 1
        assert e5_store.fetch_chunks_for_reembedding(
            tenant_id=tenant_id,
            collection="agent",
            limit=16,
        ) == []
        results = e5_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="RUSA legacy knowledge",
            query_embedding=_unit_vector_384(7),
            limit=5,
        )
        assert results
        assert results[0]["document_id"] == document_id
    finally:
        e5_store.close()
