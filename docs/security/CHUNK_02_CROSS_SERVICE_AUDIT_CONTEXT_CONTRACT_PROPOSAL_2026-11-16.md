# CHUNK 02 — Cross-Service Identity and Audit Context Contract

**Status:** PROPOSED — design only; not approved for production use  
**Date:** 2026-11-16  
**Scope:** RusaAi API ↔ RusaModels Model Server; IM19 explicitly excluded from runtime integration

## 1. Purpose

Define a trusted, tenant-scoped context that can cross the RusaAi API → RusaModels inference boundary without trusting caller-supplied tenant headers. Preserve one request identifier across the API, Model Server and configured inference runtime, and make Model Server audit records attributable to both the tenant and the originating RusaAi principal.

This proposal does not activate RAG, enable MCP direct Model Server credentials, change production secrets, apply database migrations, or authorize deployment.

## 2. Verified current-state evidence

The following observations were made from the current `main` source trees on 2026-11-16:

### RusaAi

- `rusa_agents.config.ApiPrincipal` contains `subject`, `tenant_id` and `roles`; production API-key configuration is digest-keyed.
- `llm-project/serve/app.py` authenticates the API key and derives the tenant from the authenticated principal.
- The inspected RusaAi API module does not currently install request-ID middleware. Its streaming error log reads `request.state.request_id` with an `unknown` fallback.
- `OpenAIChatInferenceProvider` sends the Model Server bearer credential but does not currently forward `X-Request-ID` or a tenant-bound signed context.
- `PostgresTaskPolicyAudit` records tenant, task, actor, action and decision, but not a request ID.
- `PostgresAuditLog` records tenant and actor in its hash-chained event payload, but has no first-class request-ID field.

### RusaModels

- `rusamodels.security.Principal` contains `subject` and `role`; it has no tenant claim.
- The Model Server API already accepts a syntactically valid `X-Request-ID`, generates one when absent/invalid, echoes it in the response and forwards it to the configured inference runtime.
- `AuditEvent` records actor subject/role, action, resource, outcome and request ID, but has no tenant column.
- The `inference` role is separate from manager/administrator roles and is limited to inference capability.

### IM19 / RAG_Cerdas

- The inspected IM19 audit table records document, event type, details and timestamp; the reviewed storage writer does not add a first-class actor or request ID.
- IM19 currently uses a single-pilot worker/approver database-role boundary and reviewer-to-PostgreSQL-login binding. It has no approved multi-tenant context contract.
- IM19 remains isolated. This proposal does not forward RusaAi tenant claims into IM19 or change its reviewer identity controls.

## 3. Security invariants

1. **Tenant authority:** `tenant_id` is derived only from the authenticated RusaAi API principal. It must never be accepted from a request body, task payload, query parameter or unsigned header.
2. **Two-factor service boundary:** RusaModels must require both its existing bearer principal with role `inference` and a valid signed RusaAi context for tenant-scoped inference calls.
3. **No privilege inheritance:** RusaAi actor roles are audit context, not authority to grant Model Server registry, artifact, quantization or administrative permissions.
4. **Audience and endpoint binding:** a signed context is valid only for the intended RusaModels audience, HTTP method, request path and request ID.
5. **Short lifetime:** signed context has a short expiry and is rejected when expired, future-dated beyond allowed clock skew, malformed or signed by an unknown key ID.
6. **Fail closed:** missing or invalid signed context on the service inference path returns an authorization failure; it must not fall back to an unsigned tenant header or a default tenant.
7. **Least disclosure:** logs and audit metadata must not contain API tokens, signing secrets, prompts, model outputs or other unnecessary sensitive content.
8. **System actions:** direct Model Server manager/administrator operations may remain system-scoped with a null tenant. They must still record their authenticated Model Server principal and request ID.
9. **IM19 isolation:** no shared service identity, tenant propagation or production connection is introduced into IM19 by this contract.
10. **Audit attribution is not data isolation:** this context adds trusted tenant attribution to inference audit events; it does not make the Model Server model registry, artifact store or job APIs tenant-partitioned. Those resource-authorization boundaries require a separate design and negative tests.

## 4. Proposed signed context

RusaAi should issue a compact, canonical JSON claim set and HMAC-SHA-256 signature for each outbound Model Server inference request.

Proposed claims:

| Claim | Meaning |
|---|---|
| `v` | Contract version; initially `1` |
| `iss` | Fixed issuer `rusa-ai` |
| `aud` | Fixed audience `rusamodels` |
| `tenant_id` | Tenant from the authenticated RusaAi API principal |
| `actor_subject` | Authenticated RusaAi principal subject |
| `actor_roles` | Authenticated RusaAi principal roles, for audit only |
| `request_id` | Request ID generated/validated at the RusaAi API boundary |
| `method`, `path` | Exact outbound HTTP method and Model Server path |
| `iat`, `exp` | Issued-at and expiry Unix timestamps |
| `jti` | Random per-request identifier |
| `service` | Fixed service identity `rusa-ai-api` |

Proposed headers:

- `X-Request-ID`: the same request ID as the signed claims.
- `X-Rusa-Context`: base64url-encoded canonical claim JSON.
- `X-Rusa-Context-Key-ID`: non-secret key identifier.
- `X-Rusa-Context-Signature`: lowercase hex HMAC-SHA-256.

The signature input must bind the key ID, HTTP method, exact request path and encoded claims. RusaModels must compare the request method, path and `X-Request-ID` against the signed claims after signature verification. Use constant-time signature comparison.

Initial proposed maximum context lifetime: 60 seconds, with a strict maximum accepted lifetime of 120 seconds and bounded clock skew. The exact values must be covered by tests and agreed by service owners before implementation. RusaModels should reject a reused `jti` during its validity window using a shared replay cache or database-backed nonce table; an in-memory-only cache is insufficient for a multi-worker Model Server. If replay protection is deferred, that limitation must be explicitly accepted and inference idempotency/abuse controls must be documented before production enablement.

### Key rotation

Use a key-ID-based verification key ring on RusaModels. Rotation order:

1. Add the new key ID and secret to the RusaModels verification key ring.
2. Deploy RusaModels with both old and new keys accepted.
3. Switch RusaAi to sign with the new key ID.
4. Verify traffic and audit records.
5. Remove the old key only after the maximum context lifetime and rollback window have elapsed.

The signing/verification keys must be separate from the existing Model Server bearer API token and from upstream model-provider credentials.

## 5. Validation and authorization flow

1. RusaAi validates the caller's API key and resolves `subject`, `tenant_id` and roles from its trusted principal registry.
2. RusaAi creates or validates one request ID and places it in request state and the response header.
3. For an outbound Model Server inference call, RusaAi signs a context for the exact method/path and request ID.
4. RusaModels authenticates the bearer API token. The authenticated principal must have role `inference`.
5. RusaModels verifies the context signature, key ID, issuer, audience, method, path, request ID, tenant ID, actor, timestamps and contract version.
6. Only the verified context is attached to request state. Unsigned tenant headers are ignored.
7. The Model Server writes the tenant ID and originating actor into its audit event, while retaining the Model Server service principal as service metadata.
8. RusaModels forwards the same request ID to its configured inference runtime.
9. The response returns the same request ID through RusaModels and RusaAi.

## 6. Audit event contract

The normalized logical event should contain:

| Field | Requirement |
|---|---|
| `schema_version` | Required |
| `occurred_at` | Required UTC timestamp |
| `request_id` | Required for request-bound API events |
| `tenant_id` | Required for tenant-scoped inference; nullable for system-scoped Model Server administration |
| `actor_subject` | Required authenticated human/client subject, or explicit service actor |
| `actor_roles` | Required bounded role list or role snapshot |
| `service_subject` | Required service principal responsible for the event |
| `action` | Required stable action identifier |
| `resource` | Required resource type/identifier or endpoint |
| `outcome` | Required normalized result (for example `success`, `denied`, `failed`) |
| `metadata` | Optional allowlisted, redacted attributes only |

Implementation mapping:

- RusaModels can retain its existing `request_id`, `actor_subject`, `actor_role`, `action`, `resource` and `outcome` columns; add a nullable `tenant_id` column and keep the service principal in bounded metadata. Inference events authenticated as the dedicated `inference` principal must have a verified tenant context.
- RusaAi task-policy audit should add a first-class request ID for API-originated policy events. Worker execution events must preserve the originating request/task correlation using a trusted queue column or an explicitly documented task correlation ID; never recover it from mutable JSON payload fields.
- The hash-chained RusaAi audit store should include request ID in its canonical hashed event payload before the event is signed.
- IM19 audit schema remains unchanged in this phase. Its future audit enrichment requires a separate IM19 design and approval.

## 7. Required negative and integration tests

### RusaAi

- Invalid/malformed inbound `X-Request-ID` is replaced with a generated ID; response contains the selected ID.
- A valid ID is preserved across the RusaAi → RusaModels request and response.
- Tenant and actor claims are taken from the authenticated API principal, never request JSON or caller-supplied tenant headers.
- Missing signing key or key ID fails production startup/readiness.
- Signature generation uses a new unique `jti` per outbound request.

### RusaModels

- Missing context on an inference-role chat/embedding request is denied.
- Unknown key ID, bad signature, expired/future context, wrong issuer/audience/version, wrong method/path, request-ID mismatch and malformed tenant/actor claims are denied.
- A reused `jti` within its validity window is denied across Model Server workers.
- A valid signature with an unsigned or forged tenant header does not change the verified tenant.
- Manager/administrator system-scoped operations retain their explicit non-tenant behavior and cannot impersonate an inference context.
- The audit row stores the verified tenant, originating actor and the same request ID.
- The same request ID reaches the configured upstream inference runtime.

### Cross-service

- A request from tenant A is audited as tenant A in both services.
- Tampering with tenant A's signed claims to tenant B invalidates the signature.
- A token signed for one endpoint cannot be replayed as a valid context for a different endpoint.
- RusaModels refuses a request when the inference bearer principal is valid but the signed context is absent or invalid.
- No test uses production secrets or production data; integration tests use disposable databases and generated keys.

## 8. Implementation sequence

| Step | Repository | Work | Gate |
|---|---|---|---|
| A | RusaAi | Request-ID middleware, signed context issuer, strict production config, provider propagation | Unit tests and production-config negative tests |
| B | RusaModels | Verification key ring, context verifier, inference dependency enforcement, tenant-aware audit migration | Unit tests, migration tests and PostgreSQL integration tests |
| C | RusaAi + RusaModels | Cross-service contract tests and end-to-end audit correlation | Exact-head CI in both repositories |
| D | Operations | Add/rotate secrets through approved secret manager; apply migration using migration identity; verify deployed config | Owner-approved change window and deployment evidence |
| E | RAG_Cerdas / IM19 | No action in this phase | Remains isolated and single-pilot |

## 9. Open decisions requiring owner approval

- Approve the HMAC context format, header names, claim set, maximum lifetime and key rotation policy.
- Confirm whether the Model Server audit actor role should record the originating RusaAi roles as a list in metadata or as a normalized role snapshot column.
- Confirm the service-owned secret-manager location and deployment owners for signing/verification keys.
- Confirm the retention and access policy for tenant-attributed Model Server audit records.
- Decide whether worker queue rows should persist an originating request ID as a dedicated immutable column.

## 10. Release posture

This document is a proposal, not an approved security control. No code, database schema, production secret, database grant, deployment or external integration is changed by this document. CHUNK 02 remains IN_PROGRESS and production remains NO-GO / NOT_VERIFIED until the approved contract is implemented, tested, deployed and independently verified.
