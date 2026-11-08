# RUSA per-agent RAG evaluation

`agent_gold_set.template.json` is a blank curation template for the 20 documented architecture and Agentic DevOps profiles. It is not a benchmark and contains no fabricated relevance labels.

## Labeling procedure

1. Run `python -m rusa_knowledge.seed` and `python -m rusa_knowledge.agent_mining` in preflight mode using the exact owner-supplied source files.
2. Review each extracted section against its original source locator.
3. Add realistic questions that the named agent is expected to answer.
4. Add one or more `relevant_source_uris` only when the mapped section directly supports the answer.
5. Record a reviewer note explaining the evidence relationship and set `review_status` to `APPROVED`.
6. Keep questions with ambiguous, missing, or conflicting evidence out of the positive relevance set until reviewed.
7. Keep the six AICode runtime worker profiles separate until their implementation-source maps and gold sets are reviewed.

The current offline evaluator accepts either `relevant_document_ids` or stable `relevant_source_uris`. Source URI labels can be curated before database-generated UUIDs exist. Use exactly one label type per case.

## Example evaluator call

After the relevant knowledge packs are ingested, load the reviewed cases for one agent and call `evaluate_retrieval(cases, retrieve, k=5)`. The retrieval callback must use the same tenant, agent ID, and project scope as the worker runtime. Do not evaluate a role with an administrator or unscoped retriever.

## Required evaluation dimensions

The current evaluator measures Recall@K, MRR@K, and NDCG@K. Add separate reviewed checks for:
- citation/source coverage and valid source locators;
- correct abstention when no eligible evidence exists;
- unsupported-claim rate and evidence entailment;
- cross-agent, cross-project, and cross-tenant leakage (target: zero);
- latency and index freshness.

Do not claim retrieval quality from an empty template, a single test, or a successful CI run. CI proves code checks passed for a commit; it does not prove that the indexed corpus is relevant or complete.
