# Hybrid MCP and External RAG Connectivity v1.0

- Repository: StrategyG/RusaAi
- Status: normalized external RAG retrieval is available as an opt-in Agent Worker tool; MCP Agent tool orchestration and end-user connection management remain pending
- Security owner: RusaAi Policy Engine and Tool Gateway

## Runtime topology

- Local LLM and Model Access Gateway remain the reasoning/inference path.
- RusaAi may connect outbound to administrator-approved MCP servers over Streamable HTTP.
- RusaAi may connect outbound to administrator-approved external RAG providers using the normalized contract below.
- External retrieval is optional. When offline, local inference and local memory continue; remote MCP/RAG features report unavailable rather than silently switching inference providers.
- RAG_Cerdas / IM19 remains an isolated pilot. This contract does not authorize direct access to its private schema, public exposure, cross-read, cross-write, dual-write, or synchronization.

## MCP

The existing `rusa_mcp.client.MCPToolClient` supports Streamable HTTP, bearer authentication, explicit tool allowlists, argument/response size limits, disabled redirects, and `trust_env=False`. Each configured connection must additionally be governed by:

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

The autonomous coding workers can call the read-only `search_external_rag` tool when the administrator enables the connector with `RUSA_EXTERNAL_RAG_ENABLED=true`. Configure the exact endpoint, matching hostname allowlist, and bearer credential using the `RUSA_EXTERNAL_RAG_*` worker environment variables. The runtime injects the authenticated task tenant and server-derived repository project scope as retrieval filters; the model cannot override them. Returned evidence includes source URI, immutable revision, SHA-256, locator and ranking score, and is explicitly labeled untrusted in the worker transcript.

The connector is disabled by default and fails closed when incomplete or invalid. It is not yet exposed through a browser connection-management UI; credentials and endpoint policy are administrator-managed deployment secrets. Remote MCP is still a transport client foundation and is not yet exposed as an autonomous Agent tool.

## Agent routing boundary

The intended hybrid sequence is:

1. Local planner decides whether remote knowledge/tools are needed.
2. Policy checks the selected connection and action.
3. External RAG returns bounded, cited evidence; or MCP returns a tool result.
4. Local model evaluates the result as untrusted input.
5. Local Agent executes local workspace actions only through the existing sandbox/Tool Gateway.
6. Result, source citations, request ID and policy decision are audited.

The external RAG client is integrated into the autonomous coding worker tool gateway as an opt-in, tenant/project-scoped read-only retrieval tool. MCP transport remains a separate foundation pending Agent tool routing and a connection-management UI. Full end-to-end consumer hybrid setup is not yet complete.
