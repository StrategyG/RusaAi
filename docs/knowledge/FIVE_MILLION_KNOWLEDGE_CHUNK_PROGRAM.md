# Five-Million Knowledge Chunk Program — Architecture and Acceptance Plan

**Program target:** up to 5,000,000 governed, retrievable knowledge chunks  
**Repository:** `StrategyG/RusaAi`  
**Status:** architecture proposal; NOT IMPLEMENTED; NOT A CLAIM THAT 5M CHUNKS EXIST  
**Scope:** ingestion, semantic chunking, provenance, quality, indexing, retrieval, lifecycle and capacity gates.

## 1. Target definition

A chunk is a versioned, source-grounded knowledge unit with stable identity, bounded content, provenance, structural context, quality state and retrieval metadata. A row, paragraph fragment, embedding vector or generated summary alone is not a qualified chunk.

The 5M target is a maximum program-scale corpus milestone, not a reason to split documents until a numeric quota is reached. Only chunks passing source, structure, provenance, quality and deduplication gates count toward the qualified total.

Count separately:
- discovered source assets;
- parsed source versions;
- raw segments;
- candidate chunks;
- qualified canonical chunks;
- indexed chunks;
- retrievable chunks;
- rejected/quarantined chunks.

Never report one count as another.

## 2. Source and rights governance

Every source asset must have:
- stable `source_id`, canonical URI or repository locator, title and publisher/author where known;
- source type, language, edition/version, publication/update date where available;
- acquisition timestamp and acquisition method;
- rights/license/access classification and permitted processing/use;
- cryptographic content digest, parser version and extraction status;
- source trust tier and reviewer status.

Do not bypass access controls, paywalls, robots restrictions, or license terms. Do not ingest sources whose rights or permitted processing are unknown into a production corpus. Quarantine uncertain material pending review.

## 3. Canonical chunk contract

Required fields:
- `chunk_id`: deterministic identity derived from source version, structural locator, normalized content digest and chunker version;
- `source_id`, `source_version_id`, `source_digest`;
- `document_title`, `author_publisher`, `language`, `source_type`, `rights_status`, `trust_tier`;
- `locator`: page, section, heading path, paragraph/table/figure/code locator, timestamp or repository path/line range;
- `parent_chunk_id`, `sibling_order`, `document_outline_path`;
- `content`, `content_digest`, `token_count`, `chunker_version`, `parser_version`;
- `semantic_type`: definition, principle, procedure, decision, evidence, example, code, table, narrative, critique, etc.;
- `entities`, `concept_ids`, `relations`, `prerequisites`, `related_chunk_ids`;
- `quality_score` with dimension scores, `quality_status`, `review_status`;
- `embedding_model_id`, `embedding_version`, `embedding_dimension`, `embedding_status`;
- `created_at`, `updated_at`, `supersedes_chunk_id`, `tombstone_status`.

Store provenance and chunk metadata independently from embedding payloads. Never use an LLM-generated attribution as source evidence.

## 4. Hierarchical semantic chunking

Chunking policy must be parser-aware and preserve document structure.

1. Extract document structure first: chapters, headings, paragraphs, lists, tables, figures, code blocks, footnotes, references, page coordinates and timestamps.
2. Segment at semantic boundaries. Prefer complete concepts, procedures, arguments or code units over fixed character windows.
3. Apply configurable token budgets by content type. Initial experimental bands: 250–700 tokens for focused concepts; 700–1,200 for procedures or connected arguments; 100–250 for atomic definitions only when context is attached. These are starting hypotheses, not universal limits.
4. Use small overlap only where cross-boundary context is required; record overlap and do not count duplicated overlap as new knowledge.
5. Create parent/child links: document → chapter/section → semantic unit → atomic evidence. Keep the original source locator for every child.
6. Preserve tables as structured rows/columns with headers and source page; do not flatten complex tables into ambiguous prose.
7. Preserve code as complete syntactic units with language, file path, symbol and line range.
8. Generate summaries and concept labels as derived artifacts, clearly marked as model-generated and linked to the evidence chunks.
9. Reject or quarantine malformed, empty, boilerplate-only, low-information and provenance-deficient segments.
10. Version the chunker; a new version creates a new candidate set and a controlled migration, never silent mutation of historical chunks.

## 5. Deduplication and canonicalization

Use multiple layers:
- exact source and normalized-content hashes;
- near-duplicate detection using shingling/MinHash or equivalent;
- semantic similarity as a review signal, not automatic proof of identity;
- edition-aware comparison to distinguish reprints, revised editions and changed standards;
- canonical group ID with explicit source variants.

Do not collapse distinct evidence, conflicting editions, different jurisdictions or time-sensitive guidance solely because embeddings are similar. Preserve source-specific provenance even when content is canonicalized.

## 6. Quality gates and states

Lifecycle:
`DISCOVERED → RIGHTS_REVIEW → PARSED → SEGMENTED → CHUNKED → VALIDATED → DEDUPED → REVIEWED → EMBEDDED → INDEXED → ACTIVE`

Exception states:
`BLOCKED`, `QUARANTINED`, `REJECTED`, `SUPERSEDED`, `TOMBSTONED`.

A chunk counts as **qualified** only if:
- source identity and version are stable;
- rights status permits the intended processing/use;
- source locator resolves to the original source;
- content is non-empty, language-identified and structurally valid;
- no unresolved parser corruption or OCR ambiguity is hidden;
- chunk boundaries preserve a coherent unit;
- duplicate policy has been applied;
- quality and provenance checks pass;
- review requirements for its trust tier are met.

Embedding/index failures do not erase qualified chunks; report qualified and indexed counts separately.

## 7. Retrieval architecture

Use hybrid retrieval with independent, measurable components:
- lexical/BM25 for exact terminology, identifiers, code symbols and rare terms;
- vector retrieval for semantic similarity;
- metadata filters for tenant, rights, language, source type, date, trust tier and status;
- hierarchical expansion to parent/neighbor evidence when context is needed;
- cross-encoder or calibrated reranking;
- query decomposition for multi-hop questions;
- answer citations to exact chunk IDs and original source locators.

RAG returns evidence candidates, not truth. Conflicting sources must remain visible. The answer layer must distinguish source-backed facts, inference, assumptions and unknowns. Retrieval must fail closed on tenant/rights boundaries.

## 8. Storage and index separation

Keep separate logical layers:
1. source registry and source-version ledger;
2. immutable extracted text/artifacts;
3. canonical chunk metadata and provenance;
4. embedding store with model/version/dimension metadata;
5. lexical index;
6. entity/concept/relation graph;
7. quality and review ledger;
8. ingestion job, retry, checkpoint and audit ledger.

Do not store all content only inside a vector database. Vector indexes are rebuildable derived artifacts. The canonical source/chunk ledger remains authoritative for identity and provenance.

## 9. Capacity planning for five million chunks

At 5,000,000 vectors, raw float32 vector payload is approximately:
- 384 dimensions: 7.68 GB decimal;
- 768 dimensions: 15.36 GB decimal;
- 1,024 dimensions: 20.48 GB decimal.

These are vector payload estimates only. They exclude ANN index overhead, metadata, lexical indexes, replicas, WAL, backups, source text, graph edges and operational headroom. Actual capacity must be benchmarked with the selected database/index and production query profile.

Do not choose embedding dimensions before evaluating retrieval quality, latency, memory, index build time and cost on a representative corpus. Partition by workload/tenant or source domain only when measured query plans and isolation requirements justify it. Keep index versioning and rollback possible.

## 10. Ingestion and worker execution

Model ingestion as durable, idempotent jobs. A job is not complete because a worker returned success.

Recommended stages:
- discover and register source;
- rights and trust review;
- fetch or access source under approved credentials;
- parse and extract;
- structural segmentation;
- semantic chunking;
- provenance validation;
- deduplication/canonicalization;
- quality review;
- embedding;
- lexical/vector indexing;
- retrieval evaluation;
- publish/activate.

Each stage has an idempotency key, bounded retry policy, lease ownership, monotonic checkpoint, input/output digest, metrics and error classification. Retry transient failures only. Reconcile ambiguous writes by reading back the expected digest before retrying. Dead-letter permanent failures with actionable evidence.

Use RusaAi's existing queue, worker runtime, lease and audit infrastructure. Do not create a competing queue or independent control plane. Agent roles may plan and diagnose; deterministic workers perform bounded operations; policy controls access; independent validators decide acceptance.

## 11. Five-million rollout gates

| Phase | Corpus milestone | Required evidence |
|---|---:|---|
| P0 Contract and evaluation set | 1k–5k | schema, source rights, locator accuracy, human-reviewed gold set |
| P1 Controlled pilot | 25k | parser coverage, dedup precision, quality calibration, ingestion replay |
| P2 Multi-domain pilot | 100k | retrieval benchmark by domain/language/type; tenant and rights isolation |
| P3 Scale validation | 500k | throughput, backpressure, index rebuild, recovery and cost benchmark |
| P4 Production scale | 1M | SLOs, monitoring, backup/restore, release rollback, independent security review |
| P5 Target scale | 5M | sustained load/soak, retrieval quality non-regression, full lineage, capacity and cost proof |

Do not advance phases because the raw chunk count reached the milestone. Every phase requires acceptance by evidence.

## 12. Required evaluation metrics

Measure by source type, language, domain and trust tier:
- parse success and OCR confidence;
- source locator resolution accuracy;
- semantic boundary coherence (human-reviewed sample);
- exact and near-duplicate precision/recall;
- rights/provenance completeness;
- qualified / candidate / rejected ratios;
- retrieval Recall@k, Precision@k, MRR and nDCG;
- citation correctness and answer faithfulness;
- conflict detection and stale-source handling;
- tenant/rights filter leakage tests;
- ingestion throughput, retry rate, queue age and worker utilization;
- index build/rebuild time, p50/p95/p99 latency, memory and cost per million chunks.

Define domain-specific thresholds using a gold evaluation set. Do not invent a universal quality score or declare success from aggregate averages that hide weak domains.

## 13. Initial implementation backlog

- K00: inspect existing RAG/storage/ingestion code and current schema; map reuse boundaries.
- K01: agree canonical source/chunk contract and migration strategy.
- K02: build source registry, rights status and immutable version ledger.
- K03: implement parser adapters and structural locators.
- K04: implement semantic chunker with deterministic versioning and replay tests.
- K05: implement exact/near-duplicate review and canonical groups.
- K06: implement quality/provenance gates and quarantine states.
- K07: implement embedding/lexical indexing as rebuildable derived artifacts.
- K08: implement hybrid retrieval, reranking and citation-preserving context assembly.
- K09: build human-reviewed domain evaluation sets and automated regression suite.
- K10: run staged capacity benchmarks through 5M; publish measured cost/SLO report.

## 14. Definition of Done

The program is not complete until:
- all active chunks have valid source/version/rights/locator lineage;
- the 5M count is reproducible from canonical ledger queries;
- chunker/parser/index versions are traceable and replayable;
- duplicates, superseded versions and tombstones are auditable;
- hybrid retrieval meets domain-specific gold-set thresholds;
- answer citations resolve to source evidence;
- tenant and rights isolation pass adversarial tests;
- ingestion is idempotent and recoverable after worker/process/database failure;
- 5M capacity, latency, rebuild, backup/restore and cost are measured;
- operational owners approve rollout.

**Current status:** PROPOSAL ONLY. No 5M corpus, ingestion run, schema migration, embedding job or production index is claimed by this document.
