# RUSA AI — External Teacher Models and Distillation Policy

**Status:** Architecture policy; no external model training job is configured or run by this document.

## Decision

RUSA AI remains hybrid:

- Local/open-weight QLoRA candidate: Qwen3-4B-Instruct-2507, pinned by the existing training config.
- Current knowledge: Centralized RAG, not model-weight updates.
- External high-capability model: optional teacher/reviewer/evaluator through an approved provider adapter and API account.
- Runtime authority: RUSA identity, deterministic Tool Gateway, Policy Engine, and QA evidence. A teacher model never grants permissions.

## ChatGPT / OpenAI model clarification

ChatGPT is a hosted product, not a downloadable model-weight package. RUSA cannot add ChatGPT's proprietary weights to its local QLoRA training run or directly train ChatGPT from this repository.

An OpenAI model accessed through an authorized API may be evaluated as:
- a high-capability runtime model for complex planning/review tasks;
- a teacher that proposes candidate demonstrations or critiques;
- an independent evaluator that grades outputs against a frozen rubric.

Such outputs are **candidate data only**. They must be checked against source evidence, executable tests, tool schemas, policy, and human review before any example is promoted into a training dataset. Never treat a model-generated answer as verified merely because it came from a stronger model. Record model ID/snapshot, prompt version, date, request parameters, source citations, reviewer, and acceptance outcome.

## OpenAI fine-tuning availability constraint

As of 2026-11-08, OpenAI's official documentation says the fine-tuning platform is being wound down and is no longer accessible to new users. Eligible existing customers may create jobs until 2027-01-06, unless the base model shuts down earlier. The documented SFT models include dated GPT-4.1 family snapshots; GPT-4.1 nano fine-tuned models shut down 2026-10-23 and GPT-4.1 / mini fine-tuned models are scheduled to shut down 2027-01-14.

Therefore:
- Do not make OpenAI fine-tuning a required RUSA dependency.
- Do not label GPT-6/ChatGPT as a trainable downloadable base model.
- If the account is an eligible existing fine-tuning customer, verify current model limits and shutdown dates through the authenticated organization API before creating a job. Do not assume eligibility.
- Keep Qwen QLoRA as the reproducible self-hosted training path.
- Use an OpenAI API model for teacher/evaluation only if the owner configures a supported model/API entitlement and approves data transfer.

Official references:
- https://developers.openai.com/api/docs/guides/supervised-fine-tuning
- https://developers.openai.com/api/docs/guides/model-optimization
- https://help.openai.com/en/articles/11162441-getting-started-with-fine-tuning

## Teacher-generated data governance

1. Teacher prompt must include only the minimum authorized and redacted context.
2. Private source code or tenant data must not be sent to an external provider without an explicit data-processing and owner approval basis.
3. Teacher response must cite repository/source locators; uncited claims are rejected or marked as hypotheses.
4. For code changes, the proposed patch must pass build, tests, security checks, and independent QA on the exact commit.
5. For tool-use samples, validate every action against the current role allowlist and JSON Schema.
6. Store raw teacher output in a quarantined candidate area, not directly in the active training corpus.
7. Human reviewer must approve target response and provenance before training promotion.
8. Evaluation sets remain frozen and are never used for teacher prompt tuning or training.
9. Remove secrets and personal data before external processing, storage, embedding, or training.
10. Keep model/provider/version, prompt, source hashes, reviewer, and acceptance evidence in dataset provenance.

## Recommended use in RUSA

Use an OpenAI model as a **teacher/evaluator option**, not as the source of truth. RAG remains the factual source; MCP exposes authorized tools; local QLoRA teaches RUSA's role contracts and safe action format. Compare local Qwen adapter, unmodified base model, and any approved API teacher against the same held-out benchmark before choosing a runtime route.

## Non-claims

This policy does not assert that an OpenAI model has been connected, that an OpenAI fine-tuning job is available to this account, that teacher-generated examples are verified, or that a RUSA adapter has been trained.
