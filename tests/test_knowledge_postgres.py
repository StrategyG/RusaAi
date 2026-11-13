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
    scope_migration = open("migrations/011_rag_agent_project_scope.sql", encoding="utf-8").read()
    version_migration = open("migrations/012_rag_knowledge_version_history.sql", encoding="utf-8").read()
    governance_migration = open("migrations/013_rag_knowledge_governance.sql", encoding="utf-8").read()
    role_name = "rusa_knowledge_test_" + uuid.uuid4().hex[:12]
    role_password = secrets.token_urlsafe(32)

    with psycopg.connect(DSN, autocommit=True) as admin:
        # The test database is dedicated to CI. The migration is idempotent and must
        # never drop existing application tables or data.
        admin.execute(migration)
        admin.execute(embedding_migration)
        admin.execute(scope_migration)
        admin.execute(version_migration)
        admin.execute(governance_migration)
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
                "GRANT SELECT, INSERT, UPDATE ON "
                "public.rusa_knowledge_documents TO {}"
            ).format(sql.Identifier(role_name))
        )
        admin.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON "
                "public.rusa_knowledge_chunks TO {}"
            ).format(sql.Identifier(role_name))
        )
        admin.execute(
            sql.SQL(
                "GRANT SELECT, INSERT ON "
                "public.rusa_knowledge_document_versions, "
                "public.rusa_knowledge_version_chunks TO {}"
            ).format(sql.Identifier(role_name))
        )
        admin.execute(
            sql.SQL(
                "GRANT SELECT ON public.rusa_knowledge_publication_events TO {}"
            ).format(sql.Identifier(role_name))
        )
        admin.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION "
                "public.rusa_knowledge_version_is_published(TEXT, UUID, UUID) TO {}"
            ).format(sql.Identifier(role_name))
        )
        admin.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION "
                "public.rusa_knowledge_version_attribution(TEXT, UUID, UUID) TO {}"
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
        require_publication=False,
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
                sql.SQL("REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {}").format(
                    sql.Identifier(role_name)
                )
            )
            admin.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(
                    sql.Identifier(role_name)
                )
            )
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))


@pytest.fixture
def governance_stores(knowledge_store):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from rusa_knowledge.store import PostgresKnowledgeStore

    _, _ = knowledge_store
    role_names = {
        "reviewer": "rusa_knowledge_reviewer_login_" + uuid.uuid4().hex[:10],
        "approver": "rusa_knowledge_approver_login_" + uuid.uuid4().hex[:10],
        "publisher": "rusa_knowledge_publisher_login_" + uuid.uuid4().hex[:10],
    }
    role_passwords = {key: secrets.token_urlsafe(32) for key in role_names}
    role_memberships = {
        "reviewer": "rusa_knowledge_reviewer",
        "approver": "rusa_knowledge_approver",
        "publisher": "rusa_knowledge_publisher",
    }
    with psycopg.connect(DSN, autocommit=True) as admin:
        for key, login_name in role_names.items():
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
                    sql.Identifier(login_name),
                    sql.Literal(role_passwords[key]),
                )
            )
            admin.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(role_memberships[key]),
                    sql.Identifier(login_name),
                )
            )

    connection_options = conninfo_to_dict(DSN)
    stores = {}
    dsns = {}
    try:
        for key, login_name in role_names.items():
            options = dict(connection_options)
            options["user"] = login_name
            options["password"] = role_passwords[key]
            dsns[key] = make_conninfo(**options)
            stores[key] = PostgresKnowledgeStore(
                dsns[key],
                min_pool_size=1,
                max_pool_size=2,
                db_role=role_memberships[key],
                require_publication=True,
            )
            assert stores[key].check_ready() is True
        yield stores, dsns
    finally:
        for store in stores.values():
            store.close()
        with psycopg.connect(DSN, autocommit=True) as admin:
            for login_name in role_names.values():
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login_name)))


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
        require_publication=False,
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
        lexical_results = e5_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="legacy knowledge",
            query_embedding=_unit_vector_384(7),
            limit=5,
        )
        assert lexical_results
        assert lexical_results[0]["document_id"] == document_id
        assert lexical_results[0]["vector_similarity"] == 0.0
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


def test_agent_scoped_retrieval_includes_shared_and_own_knowledge_only(knowledge_store):
    store, _ = knowledge_store
    tenant_id = "tenant-agent-scope"
    shared_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://agent-scope/shared",
        title="Shared knowledge",
        content_hash="c" * 64,
        metadata={"knowledge_scope": "shared", "agent_ids": []},
        chunks=["scope needle shared architecture evidence"],
        embeddings=[_unit_vector(0)],
    )
    analyst_id = store.ingest_document(
        tenant_id=tenant_id,
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
        tenant_id=tenant_id,
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
        tenant_id=tenant_id,
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
        tenant_id=tenant_id,
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
        tenant_id=tenant_id,
        collection="agent",
        document_id=analyst_id,
        agent_id="aicode-analyst",
    ) is not None
    assert store.get_document(
        tenant_id=tenant_id,
        collection="agent",
        document_id=analyst_id,
        agent_id="aicode-security",
    ) is None
    assert store.get_document(
        tenant_id=tenant_id,
        collection="agent",
        document_id=security_id,
    ) is None


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


def test_agent_and_project_scope_validation_rejects_untrusted_identifiers(knowledge_store):
    store, _ = knowledge_store
    with pytest.raises(ValueError, match="agent_id is invalid"):
        store.search(
            tenant_id="tenant-a",
            collection="agent",
            query="needle",
            query_embedding=_unit_vector(0),
            agent_id="analyst/../../security",
        )
    with pytest.raises(ValueError, match="project_id is invalid"):
        store.search(
            tenant_id="tenant-a",
            collection="agent",
            query="needle",
            query_embedding=_unit_vector(0),
            agent_id="aicode-analyst",
            project_id="../other-project",
        )


def test_document_versions_are_immutable_and_idempotent(knowledge_store):
    import psycopg

    store, restricted_dsn = knowledge_store
    tenant_id = "tenant-document-versions"
    document_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://versions/guide",
        title="Versioned guide",
        content_hash="a" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["original immutable content"],
        embeddings=[_unit_vector(0)],
    )
    first_versions = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    )
    assert len(first_versions) == 1
    first_version = first_versions[0]
    assert first_version["version_number"] == 1
    assert first_version["is_current"] is True

    same_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://versions/guide",
        title="Versioned guide",
        content_hash="b" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["updated immutable content"],
        embeddings=[_unit_vector(1)],
    )
    assert same_id == document_id
    versions = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    )
    assert [item["version_number"] for item in versions] == [2, 1]
    assert versions[0]["is_current"] is True
    assert versions[1]["is_current"] is False

    # Re-ingesting a historical fingerprint must not roll current_version_id
    # back from version 2 to version 1.
    store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://versions/guide",
        title="Versioned guide",
        content_hash="a" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["original immutable content"],
        embeddings=[_unit_vector(0)],
    )
    after_historical_reingest = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    )
    assert [item["version_number"] for item in after_historical_reingest] == [2, 1]
    assert after_historical_reingest[0]["is_current"] is True
    assert after_historical_reingest[1]["is_current"] is False

    original = store.get_document_version(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        version_id=first_version["version_id"],
    )
    current = store.get_document_version(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        version_id=versions[0]["version_id"],
    )
    assert original["content"] == "original immutable content"
    assert current["content"] == "updated immutable content"
    assert original["is_current"] is False
    assert current["is_current"] is True

    # Re-ingesting the exact same fingerprint does not create a duplicate version.
    store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://versions/guide",
        title="Versioned guide",
        content_hash="b" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["updated immutable content"],
        embeddings=[_unit_vector(1)],
    )
    assert len(
        store.list_document_versions(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        )
    ) == 2

    with psycopg.connect(restricted_dsn, autocommit=True) as connection:
        connection.execute("SELECT set_config('rusa.tenant_id', %s, false)", (tenant_id,))
        privileges = connection.execute(
            """
            SELECT
                has_table_privilege(current_user, 'public.rusa_knowledge_document_versions', 'UPDATE'),
                has_table_privilege(current_user, 'public.rusa_knowledge_document_versions', 'DELETE'),
                has_table_privilege(current_user, 'public.rusa_knowledge_version_chunks', 'UPDATE'),
                has_table_privilege(current_user, 'public.rusa_knowledge_version_chunks', 'DELETE')
            """
        ).fetchone()
    assert privileges == (False, False, False, False)


def test_agent_scoped_version_history_preserves_version_visibility(knowledge_store):
    store, _ = knowledge_store
    tenant_id = "tenant-version-scope"
    document_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://version-scope/analyst",
        title="Analyst knowledge",
        content_hash="c" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-analyst"],
            "project_id": "project-alpha",
        },
        chunks=["analyst project alpha original"],
        embeddings=[_unit_vector(0)],
    )
    first = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        agent_id="aicode-analyst",
        project_id="project-alpha",
    )
    assert len(first) == 1
    assert store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        agent_id="aicode-security",
        project_id="project-alpha",
    ) == []
    assert store.get_document_version(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        version_id=first[0]["version_id"],
        agent_id="aicode-analyst",
        project_id="project-beta",
    ) is None


def test_chunk_content_participates_in_immutable_version_fingerprint(knowledge_store):
    store, _ = knowledge_store
    tenant_id = "tenant-chunk-fingerprint"
    document_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://chunk-fingerprint/guide",
        title="Fingerprint guide",
        content_hash="a" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["original immutable chunk"],
        embeddings=[_unit_vector(0)],
    )
    first = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    )[0]

    # A caller cannot reuse a claimed source hash to replace content in-place.
    store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://chunk-fingerprint/guide",
        title="Fingerprint guide",
        content_hash="a" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["different chunk with the same claimed source hash"],
        embeddings=[_unit_vector(1)],
    )
    versions = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    )
    assert len(versions) == 2
    assert versions[0]["version_id"] != first["version_id"]

    # A changed embedding snapshot also receives a new version identity.
    store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://chunk-fingerprint/guide",
        title="Fingerprint guide",
        content_hash="a" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["different chunk with the same claimed source hash"],
        embeddings=[_unit_vector(2)],
    )
    versions = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    )
    assert len(versions) == 3
    assert versions[0]["version_id"] != versions[1]["version_id"]

    current = store.get_document_version(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        version_id=versions[0]["version_id"],
    )
    historical = store.get_document_version(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        version_id=first["version_id"],
    )
    assert current["content"] == "different chunk with the same claimed source hash"
    assert historical["content"] == "original immutable chunk"


def test_answer_scope_and_metadata_use_immutable_version_snapshot(knowledge_store):
    store, _ = knowledge_store
    tenant_id = "tenant-immutable-scope"
    document_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://immutable-scope/guide",
        title="Immutable scope guide",
        content_hash="c" * 64,
        metadata={
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-analyst"],
            "project_id": "project-alpha",
        },
        chunks=["immutable agent-specific source passage"],
        embeddings=[_unit_vector(0)],
    )

    # Mutable document metadata cannot downgrade the immutable version's scope
    # or replace the title/source shown in answer-facing results.
    with store._pool.connection() as connection:
        store._set_tenant(connection, tenant_id)
        connection.execute(
            """
            UPDATE public.rusa_knowledge_documents
            SET collection='tampered', source_uri='kb://tampered/guide',
                title='Tampered title',
                metadata='{"knowledge_scope":"shared"}'::jsonb
            WHERE tenant_id=%s AND document_id=%s::uuid
            """,
            (tenant_id, document_id),
        )

    assert store.search(
        tenant_id=tenant_id,
        collection="agent",
        query="immutable source passage",
        query_embedding=_unit_vector(0),
    ) == []
    assert store.get_document(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    ) is None
    assert store.list_collections(tenant_id=tenant_id) == []

    results = store.search(
        tenant_id=tenant_id,
        collection="agent",
        query="immutable source passage",
        query_embedding=_unit_vector(0),
        agent_id="aicode-analyst",
        project_id="project-alpha",
    )
    assert len(results) == 1
    assert results[0]["title"] == "Immutable scope guide"
    assert results[0]["source_uri"] == "kb://immutable-scope/guide"
    assert results[0]["metadata"]["knowledge_scope"] == "agent_specific"

    document = store.get_document(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        agent_id="aicode-analyst",
        project_id="project-alpha",
    )
    assert document is not None
    assert document["title"] == "Immutable scope guide"
    assert document["source_uri"] == "kb://immutable-scope/guide"
    assert document["content"] == "immutable agent-specific source passage"
    assert store.list_collections(
        tenant_id=tenant_id,
        agent_id="aicode-analyst",
        project_id="project-alpha",
    ) == [{"collection": "agent", "document_count": 1}]


def test_384d_ingestion_records_version_in_its_own_vector_space(knowledge_store):
    from rusa_knowledge.store import PostgresKnowledgeStore

    legacy_store, restricted_dsn = knowledge_store
    store = PostgresKnowledgeStore(
        restricted_dsn,
        dimensions=384,
        min_pool_size=1,
        max_pool_size=2,
        db_role=legacy_store.db_role,
        require_publication=False,
    )
    try:
        tenant_id = "tenant-384-version-history"
        document_id = store.ingest_document(
            tenant_id=tenant_id,
            collection="agent",
            source_uri="kb://384-versions/guide",
            title="E5 guide",
            content_hash="d" * 64,
            metadata={"knowledge_scope": "shared"},
            chunks=["384-dimensional original passage"],
            embeddings=[_unit_vector_384(3)],
        )
        store.ingest_document(
            tenant_id=tenant_id,
            collection="agent",
            source_uri="kb://384-versions/guide",
            title="E5 guide",
            content_hash="e" * 64,
            metadata={"knowledge_scope": "shared"},
            chunks=["384-dimensional revised passage"],
            embeddings=[_unit_vector_384(4)],
        )
        versions = store.list_document_versions(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        )
        assert len(versions) == 2
        historical = store.get_document_version(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
            version_id=versions[1]["version_id"],
        )
        assert historical["content"] == "384-dimensional original passage"
    finally:
        store.close()


def test_publication_requires_cleared_rights_reviews_distinct_approval_and_publisher(
    knowledge_store, governance_stores
):
    import psycopg
    from psycopg import sql

    from rusa_knowledge.store import PostgresKnowledgeStore

    store, restricted_dsn = knowledge_store
    governance, governance_dsns = governance_stores
    tenant_id = "tenant-publication-gate"
    document_id = store.ingest_document(
        tenant_id=tenant_id,
        collection="agent",
        source_uri="kb://governance/guide",
        title="Governance guide",
        content_hash="f" * 64,
        metadata={"knowledge_scope": "shared"},
        chunks=["governance lifecycle source rights review approval publication"],
        embeddings=[_unit_vector(0)],
    )
    versions = store.list_document_versions(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
    )
    version_id = versions[0]["version_id"]

    candidate = governance["reviewer"].get_review_candidate_version(
        tenant_id=tenant_id,
        collection="agent",
        document_id=document_id,
        version_id=version_id,
    )
    assert candidate is not None
    assert candidate["publication_status"] == "candidate_only"
    assert "governance lifecycle" in candidate["content"]
    with pytest.raises(PermissionError, match="reviewer database role"):
        store.get_review_candidate_version(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
            version_id=version_id,
        )

    answer_store = PostgresKnowledgeStore(
        restricted_dsn,
        min_pool_size=1,
        max_pool_size=2,
        db_role=store.db_role,
        require_publication=True,
    )
    try:
        assert answer_store.check_ready() is True
        assert answer_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="governance lifecycle",
            query_embedding=_unit_vector(0),
        ) == []
        assert answer_store.get_document(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        ) is None
        assert answer_store.list_collections(tenant_id=tenant_id) == []

        reviewer = governance["reviewer"]
        with reviewer._pool.connection() as connection:
            reviewer._set_tenant(connection, tenant_id)
            reviewer_login = connection.execute("SELECT session_user").fetchone()[0]

        # A single human login cannot combine reviewer and approver duties.
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("GRANT rusa_knowledge_approver TO {}").format(
                    sql.Identifier(reviewer_login)
                )
            )
        try:
            with pytest.raises(Exception, match="exclusive reviewer role membership"):
                reviewer.record_review_finding(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    version_id=version_id,
                    review_type="qa",
                    decision="needs_changes",
                    finding_text="multi-role login must be rejected",
                )
        finally:
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("REVOKE rusa_knowledge_approver FROM {}").format(
                        sql.Identifier(reviewer_login)
                    )
                )

        with reviewer._pool.connection() as connection:
            reviewer._set_tenant(connection, tenant_id)
            connection.execute(
                """
                INSERT INTO public.rusa_knowledge_rights_attestations (
                    tenant_id, document_id, version_id, rights_status, source_owner,
                    license_id, internal_storage_allowed, extraction_allowed,
                    derivative_allowed, embedding_allowed, retrieval_allowed,
                    retention_policy, evidence_uri, attested_by
                ) VALUES (
                    %s, %s::uuid, %s::uuid, 'unknown', 'RUSA owner',
                    'pending-review', FALSE, FALSE, FALSE, FALSE, FALSE,
                    'pending rights review', 'evidence://pending', 'rusa_knowledge_reviewer'
                )
                """,
                (tenant_id, document_id, version_id),
            )
            stored_rights_actor = connection.execute(
                """
                SELECT attested_by FROM public.rusa_knowledge_rights_attestations
                WHERE tenant_id=%s AND document_id=%s::uuid AND version_id=%s::uuid
                ORDER BY rights_id DESC LIMIT 1
                """,
                (tenant_id, document_id, version_id),
            ).fetchone()[0]
            assert stored_rights_actor == reviewer_login

        reviewer.record_rights_attestation(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            rights_status="cleared",
            source_owner="RUSA project owner",
            license_id="internal-owner-authorized",
            attribution_required=True,
            attribution_text="Source: RUSA project owner, authorized internal knowledge base.",
            internal_storage_allowed=True,
            extraction_allowed=True,
            derivative_allowed=True,
            embedding_allowed=True,
            retrieval_allowed=True,
            retention_policy="retain while owner authorization remains valid",
            evidence_uri="approval://rights/guide-v1",
        )
        with reviewer._pool.connection() as connection:
            reviewer._set_tenant(connection, tenant_id)
            connection.execute(
                """
                INSERT INTO public.rusa_knowledge_review_findings (
                    tenant_id, document_id, version_id, review_type, decision,
                    finding_text, reviewer_principal
                ) VALUES (
                    %s, %s::uuid, %s::uuid, 'technical', 'needs_changes',
                    'actor identity override test', 'rusa_knowledge_reviewer'
                )
                """,
                (tenant_id, document_id, version_id),
            )
            stored_review_actor = connection.execute(
                """
                SELECT reviewer_principal FROM public.rusa_knowledge_review_findings
                WHERE tenant_id=%s AND document_id=%s::uuid AND version_id=%s::uuid
                ORDER BY review_id DESC LIMIT 1
                """,
                (tenant_id, document_id, version_id),
            ).fetchone()[0]
            reviewer_login = connection.execute("SELECT session_user").fetchone()[0]
            assert stored_review_actor == reviewer_login

        approver = governance["approver"]
        approver.record_human_approval(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            decision="approved",
            approval_reference="pre-review-approval-ticket",
        )

        for review_type in ("technical", "source_fidelity", "security", "qa"):
            reviewer.record_review_finding(
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                review_type=review_type,
                decision="passed",
                finding_text=f"{review_type} review passed for the pinned version",
                evidence_uri=f"review://{review_type}/guide-v1",
            )

        publisher = governance["publisher"]
        with pytest.raises(Exception, match="human approval must follow the latest rights and review evidence"):
            publisher.publish_version(
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                manifest_uri="manifest://tenant-publication/guide-v1",
                manifest_sha256="a" * 64,
            )

        with approver._pool.connection() as connection:
            approver._set_tenant(connection, tenant_id)
            approver_login = connection.execute("SELECT session_user").fetchone()[0]
            connection.execute(
                """
                INSERT INTO public.rusa_knowledge_human_approvals (
                    tenant_id, document_id, version_id, decision,
                    approval_reference, approver_principal
                ) VALUES (
                    %s, %s::uuid, %s::uuid, 'rejected',
                    'spoofed-role-principal-test', 'rusa_knowledge_approver'
                )
                """,
                (tenant_id, document_id, version_id),
            )
            stored_approver_actor = connection.execute(
                """
                SELECT approver_principal FROM public.rusa_knowledge_human_approvals
                WHERE tenant_id=%s AND document_id=%s::uuid AND version_id=%s::uuid
                  AND approval_reference='spoofed-role-principal-test'
                """,
                (tenant_id, document_id, version_id),
            ).fetchone()[0]
            assert stored_approver_actor == approver_login

        with pytest.raises(Exception, match="distinct human approver"):
            publisher.publish_version(
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                manifest_uri="manifest://tenant-publication/guide-v1",
                manifest_sha256="a" * 64,
            )

        approver.record_human_approval(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            decision="approved",
            approval_reference="human-approval-ticket-001",
        )
        publication_event_id = publisher.publish_version(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            manifest_uri="manifest://tenant-publication/guide-v1",
            manifest_sha256="a" * 64,
        )
        assert publication_event_id > 0
        with publisher._pool.connection() as connection:
            publisher._set_tenant(connection, tenant_id)
            publisher_login = connection.execute("SELECT session_user").fetchone()[0]
            stored_publisher_actor = connection.execute(
                """
                SELECT publisher_principal FROM public.rusa_knowledge_publication_events
                WHERE tenant_id=%s AND publication_event_id=%s
                """,
                (tenant_id, publication_event_id),
            ).fetchone()[0]
            assert stored_publisher_actor == publisher_login

        results = answer_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="governance lifecycle",
            query_embedding=_unit_vector(0),
        )
        assert len(results) == 1
        assert results[0]["document_id"] == document_id
        assert results[0]["attribution_required"] is True
        assert results[0]["attribution_text"] == (
            "Source: RUSA project owner, authorized internal knowledge base."
        )
        published_document = answer_store.get_document(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        )
        assert published_document is not None
        assert published_document["content"] == (
            "governance lifecycle source rights review approval publication"
        )
        assert published_document["attribution_required"] is True
        assert published_document["attribution_text"] == results[0]["attribution_text"]

        published_versions = store.list_document_versions(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        )
        published_version = answer_store.get_document_version(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
            version_id=published_versions[0]["version_id"],
        )
        assert published_version is not None
        assert published_version["attribution_required"] is True
        assert published_version["attribution_text"] == results[0]["attribution_text"]

        # The worker's mutable current-chunk table cannot override the immutable
        # content snapshot pinned by the published version.
        with store._pool.connection() as connection:
            store._set_tenant(connection, tenant_id)
            connection.execute(
                """
                UPDATE public.rusa_knowledge_chunks
                SET content=%s
                WHERE tenant_id=%s AND document_id=%s::uuid AND chunk_index=0
                """,
                ("tampered mutable chunk must not be served", tenant_id, document_id),
            )
        assert answer_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="governance lifecycle",
            query_embedding=_unit_vector(0),
        ) == []
        immutable_document = answer_store.get_document(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        )
        assert immutable_document is not None
        assert immutable_document["content"] == published_document["content"]

        with store._pool.connection() as connection:
            store._set_tenant(connection, tenant_id)
            connection.execute(
                """
                UPDATE public.rusa_knowledge_chunks
                SET content=%s
                WHERE tenant_id=%s AND document_id=%s::uuid AND chunk_index=0
                """,
                (published_document["content"], tenant_id, document_id),
            )
        assert answer_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="governance lifecycle",
            query_embedding=_unit_vector(0),
        )

        assert answer_store.list_collections(tenant_id=tenant_id) == [
            {"collection": "agent", "document_count": 1}
        ]
        assert len(
            answer_store.list_document_versions(
                tenant_id=tenant_id,
                collection="agent",
                document_id=document_id,
            )
        ) == 1

        # A worker cannot roll the current pointer back to a superseded
        # version and reactivate its old publication event.
        store.ingest_document(
            tenant_id=tenant_id,
            collection="agent",
            source_uri="kb://governance/guide",
            title="Governance guide",
            content_hash="9" * 64,
            metadata={"knowledge_scope": "shared"},
            chunks=["newer unpublished governance source version"],
            embeddings=[_unit_vector(1)],
        )
        with store._pool.connection() as connection:
            store._set_tenant(connection, tenant_id)
            connection.execute(
                """
                UPDATE public.rusa_knowledge_documents
                SET current_version_id=%s::uuid
                WHERE tenant_id=%s AND document_id=%s::uuid
                """,
                (version_id, tenant_id, document_id),
            )
            old_version_eligible = connection.execute(
                """
                SELECT public.rusa_knowledge_version_is_published(
                    %s, %s::uuid, %s::uuid
                )
                """,
                (tenant_id, document_id, version_id),
            ).fetchone()[0]
            assert old_version_eligible is False

        # A later failed review invalidates the old publication immediately,
        # even before a publisher appends an explicit withdrawal event.
        reviewer.record_review_finding(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            review_type="security",
            decision="failed",
            finding_text="New security evidence requires quarantine",
            evidence_uri="review://security/guide-v1-followup",
        )
        assert answer_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="governance lifecycle",
            query_embedding=_unit_vector(0),
        ) == []
        assert answer_store.get_document(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        ) is None

        publisher.withdraw_version(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            reason="owner requested withdrawal",
        )
        assert answer_store.search(
            tenant_id=tenant_id,
            collection="agent",
            query="governance lifecycle",
            query_embedding=_unit_vector(0),
        ) == []
        assert answer_store.get_document(
            tenant_id=tenant_id,
            collection="agent",
            document_id=document_id,
        ) is None
        assert answer_store.list_collections(tenant_id=tenant_id) == []

        with psycopg.connect(governance_dsns["publisher"]) as connection:
            connection.execute("SELECT set_config('role', %s, true)", ("rusa_knowledge_publisher",))
            connection.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))
            audit = connection.execute(
                """
                SELECT actor_principal, actor_role, action
                FROM public.rusa_knowledge_audit_events
                WHERE tenant_id=%s
                ORDER BY audit_event_id
                """,
                (tenant_id,),
            ).fetchall()
        principals = {row[0] for row in audit}
        actor_roles = {row[1] for row in audit}
        assert len(principals) == 3
        assert actor_roles == {
            "rusa_knowledge_reviewer",
            "rusa_knowledge_approver",
            "rusa_knowledge_publisher",
        }
        assert any(row[2] == "rusa_knowledge_rights_attestations.insert" for row in audit)
        assert any(row[2] == "rusa_knowledge_review_findings.insert" for row in audit)
        assert any(row[2] == "rusa_knowledge_human_approvals.insert" for row in audit)
        assert sum(row[2] == "rusa_knowledge_publication_events.insert" for row in audit) == 2
    finally:
        answer_store.close()
