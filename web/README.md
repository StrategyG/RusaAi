# RUSA AI Control Center

Next.js 15 / React 19 frontend for RUSA AI's existing FastAPI and AICode APIs.

## Local development

1. Start the RUSA API using the root repository instructions.
2. Set `RUSA_API_BASE_URL=http://127.0.0.1:8000`.
3. Set `RUSA_WEB_SESSION_SECRET` to a random secret of at least 32 characters.
4. Optionally set `RUSA_METRICS_API_KEY` for the platform-admin metrics view.
5. Install and run:

    npm install
    npm run dev

Open http://localhost:3000.

Sign in with a tenant-scoped RUSA API key. The key is encrypted into an HttpOnly, SameSite cookie and is only read by server-side route handlers. It is never returned to browser JavaScript or stored in localStorage.

## Docker

The root Compose file includes a web service. Set `RUSA_WEB_SESSION_SECRET` in the root `.env` file before starting the stack. `RUSA_METRICS_API_KEY` is passed server-side to the web container only to support platform-admin metrics access.

## Workspace coverage

- Command Center and service health.
- AICode task intake, tenant-scoped list, pagination, detail, failed-task retry, and cancellation.
- Approval queue visibility without fake approve/reject controls.
- Source-defined AICode runtime roles and the ten-agent architecture catalogue.
- Eight-stage Agentic DevOps lifecycle contract viewer.
- RUSA AI chat, model discovery, and streaming with non-stream fallback.
- Memory taxonomy, artifact/evidence references, KPI definitions, governance, and product pillars.
- Liveness/readiness and platform-admin-only Prometheus metrics.
- A2A envelope validation via the existing schema validator.

## Current capability boundaries

The backend currently has no external WebSocket endpoint, approval-decision endpoint, live agent registry, dedicated repository-browsing endpoint, memory API, or evaluation API. The UI polls task state, surfaces awaiting_approval records without approve/reject actions, and shows artifact references returned in task results. The eight-stage DevOps lifecycle and ten architecture agents remain separate from the source-defined AICode worker roles.

See [UI Control Center integration notes](../docs/UI_CONTROL_CENTER.md) and [UI/UX precision matrix](../docs/UI_UX_PRECISION_MATRIX.md).
