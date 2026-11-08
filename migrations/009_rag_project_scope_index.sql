BEGIN;

CREATE INDEX IF NOT EXISTS rusa_knowledge_documents_project_scope_idx
    ON public.rusa_knowledge_documents (
        tenant_id,
        collection,
        (metadata->>'project_id')
    )
    WHERE metadata->>'knowledge_scope' = 'agent_specific'
      AND metadata ? 'project_id';

COMMIT;
