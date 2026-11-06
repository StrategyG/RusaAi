import os
from pathlib import Path

import pytest


DSN = os.getenv("RUSA_TEST_DATABASE_URL")
if not DSN:
    pytest.skip("RUSA_TEST_DATABASE_URL is not configured", allow_module_level=True)


@pytest.fixture(scope="module")
def knowledge_store():
    import psycopg

    migration = Path("migrations/005_rag_knowledge_pgvector.sql").read_text(encoding="utf-8")
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP TABLE IF EXISTS public.rusa_knowledge_chunks CASCADE")
        connection.execute("DROP TABLE IF EXISTS public.rusa_knowledge_documents CASCADE")
        connection.execute(migration)
        connection.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'rusa_knowledge_test_api'
                ) THEN
                    CREATE ROLE rusa_knowledge_test_api NOLOGIN NOBYPASSRLS;
                END IF;
            END;
            $$
            """
        )
        connection.execute("ALTER ROLE rusa_knowledge_test_api NOBYPASSRLS")
        connection.execute(
            "GRANT SELECT ON public.rusa_knowledge_documents, "
            "public.rusa_knowledge_chunks TO rusa_knowledge_test_api"
        )

    from rusa_knowledge.store import PostgresKnowledgeStore

    store = PostgresKnowledgeStore(DSN, min_pool_size=1, max_pool_size=2)
    try:
        assert store.check_ready() is True
        yield store
    finally:
        store.close()


def _unit_vector(index: int) -> list[float]:
    vector = [0.0] * 1536
    vector[index] = 1.0
    return vector


def test_pgvector_hybrid_retrieval_and_tenant_rls(knowledge_store):
    import psycopg

    store = knowledge_store
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

    with psycopg.connect(DSN) as connection:
        connection.execute("SET ROLE rusa_knowledge_test_api")
        connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", ("tenant-a",))
        visible = connection.execute(
            "SELECT tenant_id FROM public.rusa_knowledge_documents ORDER BY tenant_id"
        ).fetchall()
    assert visible == [("tenant-a",)]
