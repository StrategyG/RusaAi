# Remote MCP and External RAG Connection Configuration v1

The JSON Schemas in this directory describe administrator-approved outbound connections. They are configuration contracts, not a user-controlled arbitrary URL fetch API.

## Security rules

- Connections are disabled by default.
- Endpoint hostnames must match an administrator-managed allowlist; URLs supplied by an LLM or ordinary user are never fetched directly.
- Credentials are referenced by secret-store identifier (`secret://...`), never embedded in a connection document, browser payload, editor settings, logs or Git.
- HTTPS/TLS verification is mandatory; redirects are disabled.
- Remote MCP tools are restricted by explicit allowlist. Read-only is the default; write-capable tools require policy approval and human approval where applicable.
- External RAG sends only the explicit query and filters. `send_local_context` is fixed false; local files, conversation history and private memory are not automatically uploaded.
- Tool output and retrieved passages are untrusted data. They cannot grant permissions or override system/policy instructions.
- Every invocation needs a request ID, tenant attribution, policy decision, timeout and bounded response size in audit.

## Runtime status

RusaAi already has a Streamable HTTP MCP client. The normalized External RAG HTTP client is implemented as a reusable transport/validation component. These schemas define the configuration shape for a future connection-management UI and per-tenant secret/allowlist registry; that UI and full Agent/Worker routing are not yet implemented.
