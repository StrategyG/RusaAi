# RUSA-AI Security Hardening

## Security contract

- All inference, task-validation and tenant-record endpoints require X-API-Key authentication.
- Authentication fails closed. An absent or malformed key registry returns HTTP 503; an absent or invalid caller key returns HTTP 401.
- RUSA_API_KEYS_JSON is a JSON object keyed by SHA-256 hex digests. Each value contains subject, tenant_id and non-empty roles. Raw API keys are generated out-of-band; only hashes belong in deployment configuration.
- Roles are platform_admin, tenant_admin, developer and read_only. Every principal remains scoped to the tenant asserted by its key; a role never grants cross-tenant access.
- The database application role must be NOSUPERUSER, NOBYPASSRLS and must not own public.tenant_records. Startup verifies role flags, ENABLE/FORCE RLS and the exact tenant policy.
- Tenant-record operations set app.tenant_id transaction-locally and use parameterized SQL. PostgreSQL RLS is a second authorization boundary.
- Production requires PostgreSQL and Redis. Redis is the shared rate-limit backend; if it is unavailable, requests fail closed. Both peer-IP authentication attempts and authenticated tenant/subject requests are rate-limited. In-memory limiting is development/test only.
- Request bodies are bounded by RUSA_MAX_REQUEST_BYTES (default 1 MiB). Validation and application errors do not echo request values, credentials, SQL or exception messages.
- A2A schema is mandatory. Missing or invalid schema returns HTTP 503, never valid=true.
- /health is a minimal public liveness endpoint. API docs and OpenAPI routes are disabled in production.

## Database provisioning

Run migrations/001_tenant_records_rls.sql as a dedicated database migration administrator. Set a strong password for rusa_app out-of-band and provide the application DSN as RUSA_DATABASE_URL. Never run the app with the migration administrator, table owner, a superuser or a role with BYPASSRLS.

The connection pool is configured with RUSA_DB_POOL_MAX (default 10). The app role receives only schema usage and SELECT/INSERT on tenant_records.

## API key provisioning

For each key, calculate its digest without placing the raw key in source control:

    printf '%s' "$NEW_API_KEY" | sha256sum

Configure only the 64-character digest:

    {
      "<sha256-hex-digest>": {
        "subject": "service-account-1",
        "tenant_id": "tenant-a",
        "roles": ["tenant_admin"]
      }
    }

Supported permissions: platform_admin and tenant_admin can invoke chat, validate tasks and read/write tenant records. developer can invoke chat, validate tasks and read tenant records. read_only can validate tasks and read tenant records. Every credential is tenant-bound.

## Runtime settings

Required in production:
- ENVIRONMENT=production
- RUSA_API_KEYS_JSON
- RUSA_DATABASE_URL (application role only)
- RUSA_REDIS_URL

Optional:
- RUSA_RATE_LIMIT_MAX_REQUESTS=60
- RUSA_RATE_LIMIT_WINDOW_SECONDS=60
- RUSA_MAX_REQUEST_BYTES=1048576
- RUSA_DB_POOL_MAX=10

## Production boundary

The current chat-completion implementation is a development mock, not real model inference. The mock is disabled in production and /v1/chat/completions returns HTTP 503 until a separately reviewed inference adapter and model weights are integrated. This branch hardens the API security boundary but does not by itself certify the complete RUSA-AI product as production-ready.

This repository does not currently expose an MCP server. MCP authentication must be implemented and tested in the MCP gateway repository if that service is in the deployment path.

## Tests

CI provisions PostgreSQL 16 and Redis 7, applies the RLS migration as an administrator, then runs authentication-negative, RBAC, A2A-schema, request-size, rate-limit, tenant-isolation and SQL-injection tests. PostgreSQL integration tests are skipped locally without RUSA_DATABASE_URL; CI always sets it.
