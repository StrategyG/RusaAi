# Hybrid MCP and External RAG Connectivity v1.0

- Repository: StrategyG/RusaAi
- Status: tenant-scoped external RAG and read-only MCP Agent tools are available as opt-in Worker integrations; end-user connection management and DNS-pinned egress remain pending
- Security owner: RusaAi Policy Engine and Tool Gateway

## Runtime topology

- Local LLM and Model Access Gateway remain the reasoning/inference path.
- RusaAi may connect outbound to administrator-approved MCP servers over Streamable HTTP.
- RusaAi may connect outbound to administrator-approved external RAG providers using the normalized contract below.
- External retrieval is optional. When offline, local inference and local memory continue; remote MCP/RAG features report unavailable rather than silently switching inference providers.
- RAG_Cerdas / IM19 remains an isolated pilot. This contract does not authorize direct access to its private schema, public exposure, cross-read, cross-write, dual-write, or synchronization.

## MCP

The `rusa_mcp.client.MCPToolClient` supports Streamable HTTP, bearer authentication, explicit tool allowlists, argument/response size limits, disabled redirects, `trust_env=False`, and exact administrator hostname allowlists for HTTPS endpoints. Each configured connection must additionally be governed by:

- Exact administrator-approved HTTPS endpoint and hostname allowlist.
- Dedicated credential per connection; no model-plane token reuse.
- Explicit tool allowlist and per-tool policy.
- Read-only default. Write, delete, deploy, shell and financial actions require explicit permission and human approval as applicable.
- TLS verification enabled; redirects disabled.
- Tool results treated as untrusted data, never as system instructions or authorization.
- Connection timeout, response-size ceiling, audit event and revocation.

Do not allow an ordinary user or model to submit an arbitrary URL for the server to fetch. This prevents the connector from becoming an SSRF proxy.

## External RAG contract v1

The new `rusa_agents.external_rag.ExternalRAGClient` accepts only allowlisted HTTPS endpoints ending in `/v1/retrieve`. It sends:

```json
{
  "schema_version": "1.0",
  "query": "question",
  "top_k": 5,
  "filters": {}
}
```

The provider returns:

```json
{
  "schema_version": "1.0",
  "results": [
    {
      "id": "stable-evidence-id",
      "text": "bounded evidence excerpt",
      "source": {
        "uri": "https://source.example/document",
        "title": "Source title",
        "revision": "immutable-revision",
        "content_sha256": "64-character-lowercase-sha256",
        "locator": "page:4 or section:heading"
      },
      "score": 0.72,
      "metadata": {}
    }
  ]
}
```

The client validates response shape, HTTPS source URI, exact revision, SHA-256 provenance, locator, bounded text and finite ranking score. Scores are ranking signals, not truth probabilities. Duplicate evidence IDs with identical content hashes are removed. Invalid or unprovenanced results are rejected.

The connector does not automatically send local files, chat history, prompts beyond the explicit query, or private memory. Data minimization and tenant/provider authorization must be configured before enabling a connection. Provider credentials remain server-side.

### Agent Worker integration

The autonomous coding workers can call the read-only `search_external_rag` tool when the administrator configures a tenant entry in `RUSA_EXTERNAL_RAG_CONNECTIONS_JSON`. Each entry contains the exact endpoint, matching hostname allowlist, a unique `credential_env` reference, and bounded timeout/response settings. Store each bearer token in its own Worker-only environment variable, never in the JSON map; the runtime rejects reused credential variable names or token values across tenants. The runtime injects the authenticated task tenant and server-derived repository project scope as retrieval filters; the model cannot override them. Returned evidence includes source URI, immutable revision, SHA-256, locator and ranking score, and is explicitly labeled untrusted in the worker transcript.

The connector is disabled by default and fails closed when incomplete or invalid. When no connector is configured, the remote tool is omitted from the Agent prompt. If a configured provider becomes unavailable, the tool returns a bounded non-fatal unavailable result so the local planner can continue with local inference and local knowledge. It is not yet exposed through a browser connection-management UI; credentials and endpoint policy are administrator-managed deployment secrets.

### MCP Agent Worker integration

For a deployment that enables the Worker profile, copy `.env.worker.example` to `.env.worker`. Compose loads this file only into the Agent Worker; the API and web containers do not receive its connector credentials. Keep `.env.worker` out of version control.

The autonomous workers expose `list_mcp_tools` and `call_mcp_tool` only when a connection exists for the authenticated task tenant. Configure tenant-keyed `RUSA_MCP_CONNECTIONS_JSON` in the Worker-only `.env.worker` file. Each entry contains the exact HTTPS endpoint, matching `allowed_hosts`, a `credential_env` reference, and an explicit `read_only_tools` list. The raw bearer token belongs in the separately named environment variable, never in the JSON mapping. Optional internal HTTP endpoints require both `allowed_hosts` and `allow_http_hosts`; public endpoints must use HTTPS.

The runtime builds one MCP client per tenant and injects only the matching client into that tenant's Agent tool gateway. MCP discovery metadata and tool results are labeled untrusted. A provider outage returns a non-fatal unavailable result; it does not redirect local inference. The Worker image includes the MCP client runtime dependencies. Administrators must ensure the configured allowlist contains only read-only tools; write-capable tools are not supported by this integration.


## Agent routing boundary

The intended hybrid sequence is:

1. Local planner decides whether remote knowledge/tools are needed.
2. Policy checks the selected connection and action.
3. External RAG returns bounded, cited evidence; or MCP returns a tool result.
4. Local model evaluates the result as untrusted input.
5. Local Agent executes local workspace actions only through the existing sandbox/Tool Gateway.
6. Result, source citations, request ID and policy decision are audited.

External RAG and remote MCP are integrated into the autonomous coding worker tool gateway as opt-in read-only tools. External RAG filters are injected from the authenticated tenant and server-derived repository project; MCP clients are configured and isolated per tenant. A browser connection-management UI, DNS-pinned outbound egress gateway, and full end-to-end consumer hybrid setup remain pending.
