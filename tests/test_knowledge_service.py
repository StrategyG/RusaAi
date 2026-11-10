from rusa_knowledge.service import KnowledgeService


class FakeStore:
    def __init__(self):
        self.ingested = None
        self.search_args = None

    def ingest_document(self, **kwargs):
        self.ingested = kwargs
        return "00000000-0000-0000-0000-000000000001"

    def search(self, **kwargs):
        self.search_args = kwargs
        return [{"document_id": "doc-1", "content": "evidence"}]

    def get_document(self, **kwargs):
        return {"document_id": kwargs["document_id"]}

    def list_collections(self, **kwargs):
        return [{"collection": "agent", "document_count": 1}]

    def close(self):
        pass


class FakeEmbeddings:
    def __init__(self):
        self.inputs = []
        self.batches = []

    def embed(self, texts):
        self.inputs.extend(texts)
        self.batches.append(len(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_ingest_text_hashes_and_indexes_chunks():
    store = FakeStore()
    embeddings = FakeEmbeddings()
    service = KnowledgeService(store, embeddings)
    result = service.ingest_text(
        tenant_id="tenant-a",
        collection="agent",
        source_uri="file:///guide.txt",
        title="Guide",
        content="One paragraph of useful knowledge.",
    )
    assert result["document_id"].endswith("0001")
    assert result["chunk_count"] == 1
    assert len(result["content_hash"]) == 64
    assert store.ingested["tenant_id"] == "tenant-a"
    assert store.ingested["chunks"] == ["One paragraph of useful knowledge."]
    assert len(store.ingested["embeddings"][0]) == 3


def test_search_embeds_query_and_returns_store_results():
    store = FakeStore()
    embeddings = FakeEmbeddings()
    service = KnowledgeService(store, embeddings)
    results = service.search(
        tenant_id="tenant-a",
        collection="agent",
        query="How does RUSA work?",
        limit=3,
    )
    assert results[0]["content"] == "evidence"
    assert store.search_args["tenant_id"] == "tenant-a"
    assert store.search_args["query_embedding"] == [0.1, 0.2, 0.3]



def test_ingestion_blocks_high_confidence_credentials_before_embedding():
    store = FakeStore()
    embeddings = FakeEmbeddings()
    service = KnowledgeService(store, embeddings)
    secret_document = (
        "Internal notes\n-----BEGIN RSA PRIVATE KEY-----\n"
        + ("A" * 64)
        + "\n-----END RSA PRIVATE KEY-----"
    )
    try:
        service.ingest_text(
            tenant_id="tenant-a",
            collection="agent",
            source_uri="file:///secrets.txt",
            title="Secrets",
            content=secret_document,
        )
    except ValueError as exc:
        assert "private-key material" in str(exc)
    else:
        raise AssertionError("credential-bearing document was ingested")
    assert embeddings.inputs == []
    assert store.ingested is None



def test_ingest_text_batches_large_documents_with_provider_limit():
    store = FakeStore()
    embeddings = FakeEmbeddings()
    service = KnowledgeService(store, embeddings)
    result = service.ingest_text(
        tenant_id="tenant-a",
        collection="agent",
        source_uri="file:///large-guide.txt",
        title="Large Guide",
        content="x" * 90_000,
    )
    assert result["chunk_count"] > 64
    assert len(embeddings.batches) >= 2
    assert max(embeddings.batches) <= 64
    assert len(store.ingested["embeddings"]) == result["chunk_count"]


def test_ingestion_blocks_credentials_in_metadata_before_embedding():
    store = FakeStore()
    embeddings = FakeEmbeddings()
    service = KnowledgeService(store, embeddings)
    secret = "ghp_" + ("d" * 36)
    try:
        service.ingest_text(
            tenant_id="tenant-a",
            collection="agent",
            source_uri="file:///guide.txt",
            title="Guide",
            content="Safe document content.",
            metadata={"imported_by_token": secret},
        )
    except ValueError as exc:
        assert "GitHub credential" in str(exc)
    else:
        raise AssertionError("credential-bearing metadata was ingested")
    assert embeddings.inputs == []
    assert store.ingested is None


def test_ingestion_blocks_source_uri_userinfo_before_embedding():
    store = FakeStore()
    embeddings = FakeEmbeddings()
    service = KnowledgeService(store, embeddings)
    try:
        service.ingest_text(
            tenant_id="tenant-a",
            collection="agent",
            source_uri="https://user:password@example.com/private",
            title="Guide",
            content="Safe document content.",
        )
    except ValueError as exc:
        assert "embedded credentials" in str(exc)
    else:
        raise AssertionError("credential-bearing source URI was ingested")
    assert embeddings.inputs == []
    assert store.ingested is None


def test_search_blocks_credentials_before_embedding():
    store = FakeStore()
    embeddings = FakeEmbeddings()
    service = KnowledgeService(store, embeddings)
    secret = "ghp_" + ("e" * 36)
    try:
        service.search(
            tenant_id="tenant-a",
            collection="agent",
            query=secret,
        )
    except ValueError as exc:
        assert "GitHub credential" in str(exc)
    else:
        raise AssertionError("credential-bearing search query was embedded")
    assert embeddings.inputs == []
    assert store.search_args is None


class FakeTypedEmbeddings(FakeEmbeddings):
    input_type_enabled = True

    def __init__(self):
        super().__init__()
        self.input_types = []

    def embed(self, texts, *, input_type):
        self.input_types.append(input_type)
        return super().embed(texts)


def test_e5_ingestion_uses_passage_and_retrieval_uses_query_mode():
    store = FakeStore()
    embeddings = FakeTypedEmbeddings()
    service = KnowledgeService(store, embeddings)
    service.ingest_text(
        tenant_id="tenant-a",
        collection="agent",
        source_uri="file:///e5-guide.txt",
        title="E5 guide",
        content="RUSA agent architecture and retrieval.",
    )
    service.search(
        tenant_id="tenant-a",
        collection="agent",
        query="How does retrieval work?",
    )
    assert embeddings.input_types == ["passage", "query"]


def test_embed_passages_exposes_typed_embedding_for_backfill():
    embeddings = FakeTypedEmbeddings()
    service = KnowledgeService(FakeStore(), embeddings)
    assert service.embed_passages(["one passage"]) == [[0.1, 0.2, 0.3]]
    assert embeddings.input_types == ["passage"]
