BEGIN;

-- Add a second vector space without deleting or rewriting the existing 1536d data.
-- Legacy rows remain searchable by the existing provider until they are re-embedded.
ALTER TABLE public.rusa_knowledge_chunks
    ADD COLUMN IF NOT EXISTS embedding_384 vector(384);

ALTER TABLE public.rusa_knowledge_chunks
    ALTER COLUMN embedding DROP NOT NULL;

DO $rusa$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.rusa_knowledge_chunks'::regclass
          AND conname = 'rusa_knowledge_chunks_embedding_present'
    ) THEN
        ALTER TABLE public.rusa_knowledge_chunks
            ADD CONSTRAINT rusa_knowledge_chunks_embedding_present
            CHECK (embedding IS NOT NULL OR embedding_384 IS NOT NULL)
            NOT VALID;
    END IF;
END;
$rusa$;

ALTER TABLE public.rusa_knowledge_chunks
    VALIDATE CONSTRAINT rusa_knowledge_chunks_embedding_present;

CREATE INDEX IF NOT EXISTS rusa_knowledge_chunks_embedding_384_hnsw_idx
    ON public.rusa_knowledge_chunks USING hnsw (embedding_384 vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMENT ON COLUMN public.rusa_knowledge_chunks.embedding IS
    'Legacy 1536-dimensional embedding space. Retained for backward compatibility.';
COMMENT ON COLUMN public.rusa_knowledge_chunks.embedding_384 IS
    '384-dimensional multilingual-e5-small embedding space. Backfilled by scripts/reembed_knowledge.py.';

COMMIT;
