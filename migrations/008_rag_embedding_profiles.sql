BEGIN;

CREATE TABLE IF NOT EXISTS public.rusa_knowledge_collection_profiles (
    tenant_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    native_dimensions INTEGER NOT NULL,
    output_dimensions INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, collection),
    CONSTRAINT rusa_knowledge_profiles_collection_check
        CHECK (collection ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    CONSTRAINT rusa_knowledge_profiles_provider_check
        CHECK (length(embedding_provider) BETWEEN 1 AND 64),
    CONSTRAINT rusa_knowledge_profiles_model_check
        CHECK (length(embedding_model) BETWEEN 1 AND 200),
    CONSTRAINT rusa_knowledge_profiles_dimensions_check
        CHECK (native_dimensions BETWEEN 1 AND output_dimensions AND output_dimensions <= 8192)
);

-- Existing unversioned collections are marked legacy to prevent mixed-model retrieval.
INSERT INTO public.rusa_knowledge_collection_profiles
    (tenant_id, collection, embedding_provider, embedding_model,
     native_dimensions, output_dimensions)
SELECT DISTINCT tenant_id, collection, 'legacy', 'unversioned', 1536, 1536
FROM public.rusa_knowledge_documents
ON CONFLICT (tenant_id, collection) DO NOTHING;

ALTER TABLE public.rusa_knowledge_collection_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rusa_knowledge_collection_profiles FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rusa_knowledge_profiles_tenant_isolation
    ON public.rusa_knowledge_collection_profiles;
CREATE POLICY rusa_knowledge_profiles_tenant_isolation
    ON public.rusa_knowledge_collection_profiles
    USING (tenant_id = current_setting('rusa.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('rusa.tenant_id', true));

DO $rusa$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_knowledge_worker') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON public.rusa_knowledge_collection_profiles TO rusa_knowledge_worker;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_api_runtime') THEN
        GRANT USAGE ON SCHEMA public TO rusa_api_runtime;
        GRANT SELECT ON public.rusa_knowledge_collection_profiles TO rusa_api_runtime;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_rag_runtime') THEN
        ALTER ROLE rusa_rag_runtime
            NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
        GRANT rusa_knowledge_worker TO rusa_rag_runtime WITH INHERIT FALSE;
        EXECUTE format(
            'GRANT CONNECT ON DATABASE %I TO rusa_rag_runtime',
            current_database()
        );
    END IF;
END;
$rusa$;

COMMIT;
