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
    agent_scope_indexes = open("migrations/008_rag_agent_scope_indexes.sql", encoding="utf-8").read()
    project_scope_index = open("migrations/009_rag_project_scope_index.sql", encoding="utf-8").read()
    version_history = open("migrations/010_rag_knowledge_version_history.sql", encoding="utf-8").read()
    role_name = "rusa_knowledge_test_" + uuid.uuid4().hex[:12]
    role_password = secrets.token_urlsafe(32)

    with psycopg.connect(DSN, autocommit=True) as admin:
        # The test database is dedicated to CI. The migration is idempotent and must
        # never drop existing application tables or data.
        admin.execute(migration)
        admin.execute(agent_scope_indexes)
        admin.execute(project_scope_index)
        admin.execute(version_history)
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


def test_agent_scoped_retrieval_includes_shared_and_own_knowledge_only(knowledge_store):
    store, _ = knowledge_store
    shared_id = store.ingest_document(
        tenant_id="tenant-agent-scope",
        collection="agent",
        source_uri="kb://agent-scope/shared",
        title="Shared knowledge",
        content_hash="c" * 64,
        metadata={"knowledge_scope": "shared", "agent_ids": []},
        chunks=["scope needle shared architecture evidence"],
        embeddings=[_unit_vector(0)],
    )
    analyst_id = store.ingest_document(
        tenant_id="tenant-agent-scope",
        collection="agent",
        source_uri="kb://agent-scope/analyst",
        title="Analyst private knowledge",
        content_hash="d" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-analyst"],
        },
        chunks=["scope needle analyst-only task analysis evidence"],
        embeddings=[_unit_vector(0)],
    )
    security_id = store.ingest_document(
        tenant_id="tenant-agent-scope",
        collection="agent",
        source_uri="kb://agent-scope/security",
        title="Security private knowledge",
        content_hash="e" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-security"],
        },
        chunks=["scope needle security-only threat analysis evidence"],
        embeddings=[_unit_vector(0)],
    )

    analyst_results = store.search(
        tenant_id="tenant-agent-scope",
        collection="agent",
        query="scope needle",
        query_embedding=_unit_vector(0),
        limit=10,
        agent_id="aicode-analyst",
    )
    analyst_ids = {item["document_id"] for item in analyst_results}
    assert shared_id in analyst_ids
    assert analyst_id in analyst_ids
    assert security_id not in analyst_ids

    shared_results = store.search(
        tenant_id="tenant-agent-scope",
        collection="agent",
        query="scope needle",
        query_embedding=_unit_vector(0),
        limit=10,
    )
    shared_result_ids = {item["document_id"] for item in shared_results}
    assert shared_id in shared_result_ids
    assert analyst_id not in shared_result_ids
    assert security_id not in shared_result_ids

    assert store.get_document(
        tenant_id="tenant-agent-scope",
        collection="agent",
        document_id=analyst_id,
        agent_id="aicode-analyst",
    ) is not None
    assert store.get_document(
        tenant_id="tenant-agent-scope",
        collection="agent",
        document_id=analyst_id,
        agent_id="aicode-security",
    ) is None
    assert store.get_document(
        tenant_id="tenant-agent-scope",
        collection="agent",
        document_id=security_id,
    ) is None


def test_agent_id_validation_rejects_untrusted_invalid_identifier(knowledge_store):
    store, _ = knowledge_store
    with pytest.raises(ValueError, match="agent_id is invalid"):
        store.search(
            tenant_id="tenant-a",
            collection="agent",
            query="needle",
            query_embedding=_unit_vector(0),
            agent_id="analyst/../../security",
        )


def test_project_scoped_agent_retrieval_and_collection_counts(knowledge_store):
    store, _ = knowledge_store
    tenant_id = "tenant-agent-project"
    shared_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://agent-project/shared",
        title="Shared platform guidance",
        content_hash="1" * 64,
        metadata={"knowledge_scope": "shared", "agent_ids": []},
        chunks=["project boundary shared platform knowledge"],
        embeddings=[_unit_vector(0)],
    )
    analyst_global_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://agent-project/analyst-global",
        title="Analyst general guidance",
        content_hash="2" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-analyst"],
        },
        chunks=["project boundary analyst general knowledge"],
        embeddings=[_unit_vector(0)],
    )
    analyst_alpha_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://agent-project/analyst-alpha",
        title="Analyst project alpha guidance",
        content_hash="3" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-analyst"],
            "project_id": "project-alpha",
        },
        chunks=["project boundary analyst alpha only knowledge"],
        embeddings=[_unit_vector(0)],
    )
    analyst_beta_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://agent-project/analyst-beta",
        title="Analyst project beta guidance",
        content_hash="4" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-analyst"],
            "project_id": "project-beta",
        },
        chunks=["project boundary analyst beta only knowledge"],
        embeddings=[_unit_vector(0)],
    )
    security_alpha_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://agent-project/security-alpha",
        title="Security project alpha guidance",
        content_hash="5" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-security"],
            "project_id": "project-alpha",
        },
        chunks=["project boundary security alpha only knowledge"],
        embeddings=[_unit_vector(0)],
    )

    alpha_results = store.search(
        tenant_id=tenant_id,
        collection="agent",
        query="project boundary",
        query_embedding=_unit_vector(0),
        limit=10,
        agent_id="aicode-analyst",
        project_id="project-alpha",
    )
    alpha_ids = {item["document_id"] for item in alpha_results}
    assert {shared_id, analyst_global_id, analyst_alpha_id} <= alpha_ids
    assert analyst_beta_id not in alpha_ids
    assert security_alpha_id not in alpha_ids

    beta_results = store.search(
        tenant_id=tenant_id,
        collection="agent",
        query="project boundary",
        query_embedding=_unit_vector(0),
        limit=10,
        agent_id="aicode-analyst",
        project_id="project-beta",
    )
    beta_ids = {item["document_id"] for item in beta_results}
    assert {shared_id, analyst_global_id, analyst_beta_id} <= beta_ids
    assert analyst_alpha_id not in beta_ids
    assert security_alpha_id not in beta_ids

    assert store.get_document(
        tenant_id=tenant_id,
        collection="agent",
        document_id=analyst_alpha_id,
        agent_id="aicode-analyst",
        project_id="project-alpha",
    ) is not None
    assert store.get_document(
        tenant_id=tenant_id,
        collection="agent",
        document_id=analyst_alpha_id,
        agent_id="aicode-analyst",
        project_id="project-beta",
    ) is None

    alpha_collections = store.list_collections(
        tenant_id=tenant_id,
        agent_id="aicode-analyst",
        project_id="project-alpha",
    )
    assert alpha_collections == [{"collection": "agent", "document_count": 3}]

    tenant_shared_collections = store.list_collections(tenant_id=tenant_id)
    assert tenant_shared_collections == [{"collection": "agent", "document_count": 1}]


def test_project_scope_validation_rejects_untrusted_identifier(knowledge_store):
    store, _ = knowledge_store
    with pytest.raises(ValueError, match="project_id is invalid"):
        store.search(
            tenant_id="tenant-a",
            collection="agent",
            query="needle",
            query_embedding=_unit_vector(0),
            agent_id="aicode-analyst",
            project_id="../other-project",
        )
