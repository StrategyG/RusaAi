# RusaModels Chat, Embedding and RAG Integration

## Status and dependency

This change prepares RUSA AI to consume the authenticated OpenAI-compatible API provided by RusaModels. The RusaModels implementation is proposed in [StrategyG/RusaModels PR #7](https://github.com/StrategyG/RusaModels/pull/7). Merge and deploy that change before enabling the 384-dimensional embedding profile.

The integration is opt-in. Existing 1536-dimensional embedding deployments continue to work until operators apply migration 009 and explicitly switch configuration.

## Ownership

- RUSA AI owns agent orchestration, Tool Gateway, Policy Engine, RAG ingestion/retrieval, memory and tenant authorization.
- RusaModels owns model artifacts, model registry, GGUF serving, embedding serving and the authenticated inference API.
- RUSA AI talks to RusaModels over HTTPS. It does not load model weights into the agent worker or Colab notebook.

## API contract

Set both base URLs to the RusaModels Model Server HTTPS URL, including the `/openai/v1` prefix:

- Chat: `POST /openai/v1/chat/completions`, model `rusa-general-4b`.
- Embeddings: `POST /openai/v1/embeddings`, model `rusa-embedding-small`.
- Model list: `GET /openai/v1/models`.

The chat provider uses the existing `RUSA_MODEL_BASE_URL`, `RUSA_MODEL_NAME` and `RUSA_MODEL_API_KEY` settings. No agent runtime or tool authorization code is moved to RusaModels.

The embedding provider uses `RUSA_EMBEDDING_BASE_URL`, `RUSA_EMBEDDING_MODEL`, `RUSA_EMBEDDING_API_KEY`, `RUSA_EMBEDDING_DIMENSIONS` and `RUSA_EMBEDDING_INPUT_TYPE_ENABLED`. For the 384d RusaModels profile, the embedding API key falls back to `RUSA_MODEL_API_KEY` when a separate key is not supplied. The legacy 1536d external embedding provider continues to use only `RUSA_EMBEDDING_API_KEY`.

For multilingual-e5-small:
- Model ID: `rusa-embedding-small`.
- Dimensions: 384.
- Document chunks use `input_type=passage`.
- Retrieval queries use `input_type=query`.
- The E5 service adds the corresponding `passage: ` or `query: ` prefix before encoding.
- The vectors are normalized by the RusaModels embedding runtime.

The default remains 1536 dimensions for backward compatibility. Do not set 384 until migration 009 is applied.

## Non-destructive vector migration

The existing schema in migration 007 stores `vector(1536)`. Migration 009 adds a parallel nullable `embedding_384 vector(384)` column and its HNSW cosine index. It relaxes the legacy column's NOT NULL constraint and adds a check requiring at least one of the two vectors.

Migration 009 does not delete or overwrite the existing 1536-dimensional vectors. During backfill, lexical retrieval can still find legacy chunks while the 384-dimensional vector index is populated. The store selects a vector column from a fixed allowlist based on the configured dimension; it does not interpolate an arbitrary environment-provided SQL identifier.

### Rollout sequence

1. Merge and deploy RusaModels PR #7. Verify both chat and embedding endpoints through the Model Server gateway.
2. Back up the RUSA AI knowledge database and confirm migration 007 is already applied.
3. Apply `migrations/009_knowledge_embedding_384.sql` using the database migration operator. Do not run schema migrations from the application worker.
4. Configure a dedicated RusaModels API token in the RUSA AI secret manager. Set:
   - `RUSA_MODEL_BASE_URL=https://<rusa-models-host>/openai/v1`
   - `RUSA_MODEL_NAME=rusa-general-4b`
   - `RUSA_MODEL_API_KEY=<dedicated-rusamodels-token>`
   - `RUSA_EMBEDDING_BASE_URL=https://<rusa-models-host>/openai/v1`
   - `RUSA_EMBEDDING_MODEL=rusa-embedding-small`
   - `RUSA_EMBEDDING_API_KEY=<dedicated-rusamodels-token>`
   - `RUSA_EMBEDDING_DIMENSIONS=384`
   - `RUSA_EMBEDDING_INPUT_TYPE_ENABLED=true`
   - `RUSA_KNOWLEDGE_DB_ROLE=rusa_knowledge_worker`
5. Start with RAG disabled. Run a read-only inventory for one tenant:
   ```bash
   python scripts/reembed_knowledge.py \
     --tenant-id <tenant-id> \
     --collection agent \
     --max-chunks 10000
   ```
6. Review the reported chunk count and expected inference cost/latency. Re-run with `--apply` to backfill only that tenant and collection:
   ```bash
   python scripts/reembed_knowledge.py \
     --tenant-id <tenant-id> \
     --collection agent \
     --max-chunks 10000 \
     --apply
   ```
   The command is dry-run by default, uses batches of at most 64, scopes all SQL by tenant, skips already migrated rows, and retains the legacy 1536d vectors. Repeat for each tenant and collection. Increase `--max-chunks` deliberately for larger corpora.
7. Verify the tenant has no legacy rows missing a 384d vector:
   ```sql
   SELECT collection, count(*) AS pending_chunks
   FROM public.rusa_knowledge_chunks
   WHERE tenant_id = '<tenant-id>'
     AND embedding IS NOT NULL
     AND embedding_384 IS NULL
   GROUP BY collection;
   ```
8. Run retrieval evaluation and a RUSA AI end-to-end task using the 384d profile. Then enable `RUSA_RAG_ENABLED=true` for the worker.

For new documents after cutover, RUSA AI writes 384d vectors to `embedding_384`. The old `embedding` column remains null for those rows. Do not mix 1536d and 384d vectors in one query or index.

## Rollback

To roll back the application to the old provider:
1. Disable RAG workers or stop ingestion during the configuration change.
2. Restore `RUSA_EMBEDDING_DIMENSIONS=1536`, the prior provider model/base URL and credentials.
3. Restart the worker and verify the 1536d knowledge store readiness.

The 1536d vectors for pre-existing chunks are retained. New documents created only in 384d mode have no legacy 1536d vector; lexical retrieval remains available for those chunks, but full vector retrieval for them requires re-embedding with the legacy provider. Migration 009 is additive and does not need to be rolled back.

## Security and networking

- Use the HTTPS forwarded Model Server URL. Do not expose llama.cpp or embedding sidecar ports directly.
- Keep the RusaModels bearer token in a secret manager; do not put it in Colab notebook source, Git, logs or dataset metadata.
- Use a dedicated least-privilege RusaModels principal and rotate its token.
- Keep RUSA AI's Tool Gateway and Policy Engine as the only runtime authorization boundary. Model output never grants tool permissions.
- Use the existing tenant-scoped database role and row-level security. The re-embedding utility requires an explicit tenant ID and never performs cross-tenant scans.
- Do not enable RAG until the selected database migration, model readiness, backfill and retrieval evaluation have succeeded.

## Operational notes

- RusaModels CPU Codespaces is a development endpoint, not an always-on production service. Codespaces suspension/expiry interrupts inference.
- The 4-vCPU/16-GB CPU profile is for inference only; QLoRA training remains a separate CUDA workflow.
- A 384-dimensional schema change requires re-embedding all searchable chunks before vector recall is complete.
- The re-embedding utility is resumable: successfully written vectors are skipped on the next run.
