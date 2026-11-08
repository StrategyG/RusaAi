BEGIN;

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_document_versions (
    tenant_id TEXT NOT NULL,
    version_id UUID NOT NULL DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL,
    collection TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    version_fingerprint CHAR(64) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, version_id),
    CONSTRAINT rusa_knowledge_versions_document_fk
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES public.rusa_knowledge_documents (tenant_id, document_id)
        ON DELETE CASCADE,
    CONSTRAINT rusa_knowledge_versions_collection_check
        CHECK (collection ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    CONSTRAINT rusa_knowledge_versions_content_hash_check
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT rusa_knowledge_versions_fingerprint_check
        CHECK (version_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT rusa_knowledge_versions_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT rusa_knowledge_versions_document_fingerprint_uq
        UNIQUE (tenant_id, document_id, version_fingerprint)
);

CREATE INDEX IF NOT EXISTS rusa_knowledge_versions_document_idx
    ON public.rusa_knowledge_document_versions
       (tenant_id, collection, document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_version_chunks (
    tenant_id TEXT NOT NULL,
    version_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1536) NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, version_id, chunk_index),
    CONSTRAINT rusa_knowledge_version_chunks_version_fk
        FOREIGN KEY (tenant_id, version_id)
        REFERENCES public.rusa_knowledge_document_versions (tenant_id, version_id)
        ON DELETE CASCADE,
    CONSTRAINT rusa_knowledge_version_chunks_index_check CHECK (chunk_index >= 0),
    CONSTRAINT rusa_knowledge_version_chunks_content_check
        CHECK (length(content) BETWEEN 1 AND 20000),
    CONSTRAINT rusa_knowledge_version_chunks_metadata_check
        CHECK (jsonb_typeof(metadata) = 'object')
);

ALTER TABLE public.rusa_knowledge_document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_document_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_version_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_version_chunks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rusa_knowledge_document_versions_tenant_isolation
    ON public.rusa_knowledge_document_versions;
CREATE POLICY rusa_knowledge_document_versions_tenant_isolation
    ON public.rusa_knowledge_document_versions
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DROP POLICY IF EXISTS rusa_knowledge_version_chunks_tenant_isolation
    ON public.rusa_knowledge_version_chunks;
CREATE POLICY rusa_knowledge_version_chunks_tenant_isolation
    ON public.rusa_knowledge_version_chunks
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

REVOKE ALL PRIVILEGES
    ON public.rusa_knowledge_document_versions FROM rusa_knowledge_worker;
REVOKE ALL PRIVILEGES
    ON public.rusa_knowledge_version_chunks FROM rusa_knowledge_worker;
GRANT SELECT, INSERT
    ON public.rusa_knowledge_document_versions TO rusa_knowledge_worker;
GRANT SELECT, INSERT
    ON public.rusa_knowledge_version_chunks TO rusa_knowledge_worker;

COMMIT;
