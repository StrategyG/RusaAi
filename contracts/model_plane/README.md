# RUSA AI Model Plane Contract

**Contract version:** 0.1.0-draft  
**Owner:** RusaAi  
**Consumer:** RusaModels Model Server  
**Status:** Draft for architecture, security and QA review

This directory is the proposed canonical contract source for the RusaAi-to-RusaModels inference boundary. It is not a claim that the custom endpoint is already implemented.

## Files

- `v0.1.0/openapi.yaml` — OpenAPI 3.1 surface for the proposed canonical endpoint.
- `v0.1.0/inference-request.schema.json` — strict request envelope.
- `v0.1.0/inference-response.schema.json` — successful response envelope.
- `v0.1.0/inference-error.schema.json` — structured error envelope.

## Ownership invariants

1. RusaAi owns end-user identity, tenant identity, workflow/run/task state, model-profile authorization, policy and Tool Gateway execution.
2. RusaModels owns model catalog/deployment metadata, technical deployment selection inside an approved profile, inference execution and model artifact lifecycle.
3. Tenant identity is authoritative only from the authenticated service credential; the request body has no tenant override.
4. Model-produced tool calls are untrusted proposals and never execute in RusaModels.
5. The repositories do not share mutable databases or import each other's private implementation modules.

## Current compatibility surface

Current RusaAi provider appends `/chat/completions` to `RUSA_MODEL_BASE_URL`. Current RusaModels Model Server exposes OpenAI-compatible routes under `/openai/v1`. The interim base URL therefore ends in `/openai/v1`, and the client model name must match the configured RusaModels model ID. RusaModels main has a least-privilege `inference` role with only `inference:invoke`.

The canonical `/model-plane/v1/inference` endpoint, tenant-bound signed identity, profile routing, policy-aware fallback, custom response metadata, distributed trace propagation, idempotency and this full envelope are proposed requirements; they are not yet implemented.

## Review gates

- Adopt canonical data-classification and region vocabularies from RusaAi policy.
- Confirm production token format and claim validation.
- Confirm payload, concurrency, token, cost and retention limits.
- Define streaming event/cancellation semantics.
- Implement positive, negative and cross-tenant tests in both repositories.
- Release this contract only after architecture, security, QA and owner approval.
