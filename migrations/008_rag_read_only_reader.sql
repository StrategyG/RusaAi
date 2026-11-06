BEGIN;

-- A read-only, non-login role for the MCP HTTP service and autonomous retrieval.
-- The ingestion CLI retains the separate write-capable rusa_knowledge_worker role.
DO $rusa$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'rusa_knowledge_reader'
    ) THEN
        CREATE ROLE rusa_knowledge_reader
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    ELSE
        ALTER ROLE rusa_knowledge_reader
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END;
$rusa$;

GRANT USAGE ON SCHEMA public TO rusa_knowledge_reader;

REVOKE ALL PRIVILEGES
    ON TABLE public.rusa_knowledge_documents, public.rusa_knowledge_chunks
    FROM rusa_knowledge_reader;
GRANT SELECT
    ON TABLE public.rusa_knowledge_documents, public.rusa_knowledge_chunks
    TO rusa_knowledge_reader;

REVOKE SELECT
    ON TABLE public.rusa_knowledge_documents, public.rusa_knowledge_chunks
    FROM rusa_api_runtime;

DROP POLICY IF EXISTS rusa_knowledge_documents_reader_tenant_isolation
    ON public.rusa_knowledge_documents;
CREATE POLICY rusa_knowledge_documents_reader_tenant_isolation
    ON public.rusa_knowledge_documents
    FOR SELECT TO rusa_knowledge_reader
    USING (tenant_id = current_setting('rusa.tenant_id', true));

DROP POLICY IF EXISTS rusa_knowledge_chunks_reader_tenant_isolation
    ON public.rusa_knowledge_chunks;
CREATE POLICY rusa_knowledge_chunks_reader_tenant_isolation
    ON public.rusa_knowledge_chunks
    FOR SELECT TO rusa_knowledge_reader
    USING (tenant_id = current_setting('rusa.tenant_id', true));

DO $rusa$
BEGIN
    -- The coding worker only searches knowledge. Remove its previous path to
    -- the write-capable ingestion role and permit SET ROLE to the reader role.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rusa_worker_runtime') THEN
        REVOKE rusa_knowledge_worker FROM rusa_worker_runtime;
        GRANT rusa_knowledge_reader TO rusa_worker_runtime WITH INHERIT FALSE;
    END IF;
END;
$rusa$;

COMMIT;
