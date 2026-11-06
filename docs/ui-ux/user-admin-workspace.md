# RUSA-AI Web Workspace: User and Admin UI

## Product surfaces

RUSA-AI now has one responsive React/TypeScript application with two role-aware workspaces:

- **User Workspace** (/ , /chat, /tasks): workspace overview, model-backed chat, personal task submission and status.
- **Admin Control Center** (/admin, /admin/users, /admin/tasks): tenant-wide overview, account provisioning, role/active-state control, and agent task operations.

The navigation is selected from the authenticated user's server-issued role. Hiding a route in the browser is only a UX decision; the API enforces the same role and tenant boundary independently.

## Request path

~~~text
Browser
  |
  | same-origin HTTPS
  v
rusa-web (unprivileged Nginx, static React build)
  |  /v1/* and /health/* reverse proxy
  v
rusa-api (FastAPI)
  |-- PostgreSQL identity and refresh sessions
  |-- PostgreSQL durable agent queue and audit log
  |-- configured local inference backend
  |
  v
rusa-agent-worker (optional agent profile)
~~~

The browser never receives the service API key, database password, provider key, or GitHub token. User requests use short-lived bearer access tokens held in memory. A rotating opaque refresh token is stored in a Secure, HttpOnly, SameSite=Strict cookie with a restricted /v1/auth path. Browser cookie-mutating requests are checked for same-origin metadata.

## API surface

| Method | Route | Access |
| --- | --- | --- |
| POST | /v1/auth/login | Public, rate-limited |
| POST | /v1/auth/refresh | Refresh cookie, same-origin |
| POST | /v1/auth/logout | Refresh cookie, same-origin |
| GET | /v1/auth/me | User |
| GET | /v1/ui/overview | User |
| GET | /v1/agent/tasks | User's own tasks |
| POST | /v1/agent/tasks | User; task is attributed to user ID |
| GET | /v1/admin/overview | Admin |
| GET/POST | /v1/admin/users | Admin |
| PATCH | /v1/admin/users/{user_id} | Admin |
| GET | /v1/admin/tasks | Admin |
| POST | /v1/admin/tasks/{task_id}/retry | Admin |
| POST | /v1/admin/tasks/{task_id}/cancel | Admin |

Existing machine-to-machine clients continue to use X-API-Key. Browser sessions use Authorization: Bearer. A bearer request is resolved to an active database user on each request, so disabling an account or changing its role invalidates stale access claims immediately.

## Tenant model

The web login is single-tenant per deployment and is scoped to RUSA_TENANT_ID. Bootstrap administrator tenant must match this value. The underlying task queue remains tenant-scoped, and service API-key tenant mapping remains available for machine clients. A multi-tenant login selector/SSO flow is a separate feature and must not be inferred from the current single-tenant login form.

## Run locally

1. Start PostgreSQL and the API with RUSA_AUTH_ENABLED=true, DATABASE_URL, RUSA_AUTH_SECRET (at least 32 bytes), RUSA_TENANT_ID, RUSA_BOOTSTRAP_ADMIN_EMAIL, and RUSA_BOOTSTRAP_ADMIN_PASSWORD.
2. Install frontend dependencies in apps/web with npm install.
3. Run npm run dev in apps/web. Vite proxies /v1 and /health to http://127.0.0.1:8000.
4. For plain HTTP local development, set RUSA_COOKIE_SECURE=false in the API environment. Open http://localhost:5173 and sign in with the bootstrap administrator.

## Run with Docker Compose

Copy .env.example to .env, replace all placeholders with unique secrets, set the model directory and local model path, then run:

~~~bash
docker compose up -d --build postgres rusa-api rusa-web
~~~

Open http://127.0.0.1:3000. For an external deployment, place a TLS-terminating reverse proxy in front of the loopback-bound web port. Do not expose the API or web container directly to the public internet without TLS and an appropriate edge policy.

To start the optional worker, configure its provider and least-privilege GitHub credentials, enable the agent runtime, then run:

~~~bash
docker compose --profile agent up -d --build rusa-agent-worker
~~~

## Security and operational notes

- Set RUSA_AUTH_SECRET to a randomly generated secret, independent from RUSA_API_KEY and POSTGRES_PASSWORD.
- The bootstrap admin is created only when its tenant/email pair is absent. Existing admin passwords are not overwritten by startup.
- Passwords are hashed with Argon2id; raw passwords and refresh tokens are not stored in the database.
- Refresh tokens rotate on use; sessions are revocable and expire.
- Login attempts are rate-limited by client IP and tenant/email.
- A user sees only tasks attributed to their user ID. Administrators can inspect tasks within their tenant only.
- The last active administrator cannot be disabled or demoted.
- Chat and task activity are not fabricated in the UI. The dashboard reflects API state and reports unavailable model/worker dependencies.
- Merge, deployment, release, secret changes, permission changes, and destructive actions remain subject to human approval under the existing agent policy.
