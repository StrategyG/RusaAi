BEGIN;

CREATE INDEX IF NOT EXISTS rusa_knowledge_documents_scope_idx
    ON public.rusa_knowledge_documents (
        (COALESCE(metadata->>'knowledge_scope', 'shared'))
    );

CREATE INDEX IF NOT EXISTS rusa_knowledge_documents_agent_ids_gin
    ON public.rusa_knowledge_documents
    USING GIN ((metadata->'agent_ids'));

COMMIT;
