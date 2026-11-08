# RUSA AI Control Center

A Next.js frontend for the existing RUSA-AI FastAPI and AICode task APIs.

## Local development

1. Start the RUSA API using the root repository instructions.
2. Set RUSA_API_BASE_URL=http://127.0.0.1:8000.
3. Set RUSA_WEB_SESSION_SECRET to a random secret of at least 32 characters.
4. Install and run:

    npm install
    npm run dev

Open http://localhost:3000.

Sign in with a tenant-scoped RUSA API key. The key is encrypted into an HttpOnly, SameSite cookie and is only read by server-side route handlers. It is never returned to browser JavaScript or stored in localStorage.

## Docker

The root Compose file includes a web service. Set RUSA_WEB_SESSION_SECRET in the root .env file before starting the stack.

## Current integration scope

- Tenant identity: GET /v1/auth/session.
- Task intake: POST /v1/aicode/tasks.
- Task history: GET /v1/aicode/tasks.
- Task detail: GET /v1/aicode/tasks/{task_id}.
- Retry: POST /v1/aicode/tasks/{task_id}/retry.
- Cancel: DELETE /v1/aicode/tasks/{task_id}.
- Chat inference: POST /v1/chat/completions.
- Health: GET /health and GET /health/ready.

The backend currently has no external WebSocket endpoint, approval-decision endpoint, or dedicated repository-browsing endpoint. The UI polls task state, surfaces awaiting_approval records without inventing approve/reject actions, and shows artifact references returned in task results. The eight-stage DevOps lifecycle is shown as an architecture target, separate from currently implemented AICode worker roles.