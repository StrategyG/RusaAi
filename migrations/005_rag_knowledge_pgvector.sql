BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_documents (
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL DEFAULT gen_random_uuid(),
    collection TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_id),
    CONSTRAINT rusa_knowledge_documents_collection_check
        CHECK (collection ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    CONSTRAINT rusa_knowledge_documents_source_check
        CHECK (length(source_uri) BETWEEN 1 AND 2048),
    CONSTRAINT rusa_knowledge_documents_title_check
        CHECK (length(title) BETWEEN 1 AND 256),
    CONSTRAINT rusa_knowledge_documents_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS rusa_knowledge_documents_source_uq
    ON public.rusa_knowledge_documents (tenant_id, collection, source_uri);

CREATE INDEX IF NOT EXISTS rusa_knowledge_documents_tenant_collection_idx
    ON public.rusa_knowledge_documents (tenant_id, collection, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_chunks (
    tenant_id TEXT NOT NULL,
    chunk_id UUID NOT NULL DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL,
    collection TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1536) NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, chunk_id),
    CONSTRAINT rusa_knowledge_chunks_document_fk
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES public.rusa_knowledge_documents (tenant_id, document_id)
        ON DELETE CASCADE,
    CONSTRAINT rusa_knowledge_chunks_index_check CHECK (chunk_index >= 0),
    CONSTRAINT rusa_knowledge_chunks_content_check
        CHECK (length(content) BETWEEN 1 AND 20000),
    CONSTRAINT rusa_knowledge_chunks_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT rusa_knowledge_chunks_document_index_uq
        UNIQUE (tenant_id, document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS rusa_knowledge_chunks_tenant_collection_idx
    ON public.rusa_knowledge_chunks (tenant_id, collection, document_id);

CREATE INDEX IF NOT EXISTS rusa_knowledge_chunks_search_gin_idx
    ON public.rusa_knowledge_chunks USING GIN (search_vector);

CREATE INDEX IF NOT EXISTS rusa_knowledge_chunks_embedding_hnsw_idx
    ON public.rusa_knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

ALTER TABLE public.rusa_knowledge_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_documents FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_chunks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rusa_knowledge_documents_tenant_isolation
    ON public.rusa_knowledge_documents;
CREATE POLICY rusa_knowledge_documents_tenant_isolation
    ON public.rusa_knowledge_documents
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DROP POLICY IF EXISTS rusa_knowledge_chunks_tenant_isolation
    ON public.rusa_knowledge_chunks;
CREATE POLICY rusa_knowledge_chunks_tenant_isolation
    ON public.rusa_knowledge_chunks
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_agent_api') THEN
        GRANT USAGE ON SCHEMA public TO rusa_agent_api;
        GRANT SELECT ON public.rusa_knowledge_documents TO rusa_agent_api;
        GRANT SELECT ON public.rusa_knowledge_chunks TO rusa_agent_api;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_agent_worker') THEN
        GRANT USAGE ON SCHEMA public TO rusa_agent_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON public.rusa_knowledge_documents TO rusa_agent_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON public.rusa_knowledge_chunks TO rusa_agent_worker;
    END IF;
END
$$;

COMMIT;
