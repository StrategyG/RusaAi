-- Agent/project scope indexes for tenant-scoped RAG retrieval.
-- Scope values are validated by the ingestion service and are always combined
-- with the authenticated tenant context enforced by the existing RLS policies.
BEGIN;

CREATE INDEX IF NOT EXISTS rusa_knowledge_documents_scope_idx
    ON public.rusa_knowledge_documents (
        (COALESCE(metadata->>'knowledge_scope', 'shared'))
    );

CREATE INDEX IF NOT EXISTS rusa_knowledge_documents_agent_ids_gin
    ON public.rusa_knowledge_documents
    USING GIN ((metadata->'agent_ids'));

CREATE INDEX IF NOT EXISTS rusa_knowledge_documents_project_scope_idx
    ON public.rusa_knowledge_documents (
        tenant_id,
        collection,
        (metadata->>'project_id')
    )
    WHERE metadata->>'knowledge_scope' = 'agent_specific'
      AND metadata ? 'project_id';

COMMIT;
