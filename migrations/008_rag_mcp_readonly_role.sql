BEGIN;

-- Dedicated MCP identity: read-only by construction. The login role used by
-- the MCP deployment must be granted membership in this NOLOGIN role.
DO $rusa$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'rusa_knowledge_mcp_reader'
    ) THEN
        CREATE ROLE rusa_knowledge_mcp_reader
            NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
    ELSE
        ALTER ROLE rusa_knowledge_mcp_reader
            NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
    END IF;
END;
$rusa$;

DO $rusa$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_auth_members
        WHERE member = (
            SELECT oid FROM pg_roles WHERE rolname = 'rusa_knowledge_mcp_reader'
        )
    ) THEN
        RAISE EXCEPTION
            'rusa_knowledge_mcp_reader must not be a member of any other role';
    END IF;
END;
$rusa$;

GRANT USAGE ON SCHEMA public TO rusa_knowledge_mcp_reader;
REVOKE ALL PRIVILEGES ON public.rusa_knowledge_documents
    FROM rusa_knowledge_mcp_reader;
REVOKE ALL PRIVILEGES ON public.rusa_knowledge_chunks
    FROM rusa_knowledge_mcp_reader;
GRANT SELECT ON public.rusa_knowledge_documents
    TO rusa_knowledge_mcp_reader;
GRANT SELECT ON public.rusa_knowledge_chunks
    TO rusa_knowledge_mcp_reader;

-- Ensure future objects do not accidentally acquire write grants for MCP.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON TABLES FROM rusa_knowledge_mcp_reader;

COMMIT;
