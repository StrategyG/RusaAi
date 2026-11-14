# Local First-Run, API Keys and Editor Onboarding v1.0

- Repository: StrategyG/RusaAi
- Status: local API-key bootstrap implementation; desktop bundle/release remains pending
- Runtime mode: opt-in via `RUSA_LOCAL_INSTALL_MODE=true`

## User flow

1. The installer starts the local RusaAi API and web control center.
2. The browser opens the local first-run page at `/setup`.
3. The web server calls the local API using a one-time setup capability kept server-side.
4. The API creates a single local owner key and returns its raw value once.
5. The page displays the local API base URL and a generic OpenAI-compatible editor configuration.
6. The user can create separate least-privilege editor keys using the local key-management API.

The API key store contains only SHA-256 digests. Raw keys are returned only at creation and are never listed or logged. Editor keys are restricted to `developer` and `read_only`; the bootstrap owner key is `tenant_admin`. The last active administrator key cannot be revoked.

## Local API configuration

```dotenv
RUSA_LOCAL_INSTALL_MODE=true
RUSA_LOCAL_API_KEYS_DB=/var/lib/rusa/local-api-keys.sqlite3
RUSA_LOCAL_SETUP_TOKEN=<random one-time setup capability>
```

The database directory must be persistent and writable only by the RusaAi API service account. The setup token is shared only between the API and server-side web process; it must never be sent to browser JavaScript, written into a client bundle, or logged.

## Endpoints

- `GET /v1/local/setup/status` — reports whether one-time local setup is complete.
- `POST /v1/local/setup/initialize` — requires `X-RUSA-Setup-Token`; creates the owner key once.
- `GET /v1/local/api-keys` — tenant-admin inventory without raw keys or digests.
- `POST /v1/local/api-keys` — creates a least-privilege editor key.
- `DELETE /v1/local/api-keys/{key_id}` — revokes a key in the authenticated tenant.

These endpoints are disabled unless local install mode is explicitly enabled. Hosted deployments continue to use the existing centrally provisioned hashed `RUSA_API_KEYS_JSON` registry.

## Web key management and VS Code preview

After first-run setup, the web server establishes an encrypted HttpOnly dashboard session. Open `/keys` to create a separate `developer` or `read_only` key for each editor and revoke keys that are no longer used. The owner key is protected from accidental revocation; the API also prevents revoking the last active tenant administrator.

A source preview of the VS Code Chat Participant is in `integrations/vscode`. It stores the API key in VS Code SecretStorage and discovers the configured model ID from `GET /v1/models` when the local model runtime is ready. The VSIX is not yet published or signed. This extension sends only the explicit chat prompt; it does not read workspace files or execute local tools.

## Editor compatibility

The local gateway base URL is `http://127.0.0.1:8000/v1` in the current Compose development profile. Clients that support a custom OpenAI-compatible endpoint can use the base URL and a dedicated editor API key. Editors requiring proprietary protocols need a dedicated extension/adapter; compatibility is not assumed merely because an editor supports AI features.

## Release and inference blockers

This implementation provides first-run identity/key provisioning, not a complete consumer installer. A signed Windows bundle, one-click dependency installation, RusaModels local runtime packaging, approved model artifact, model-specific hardware benchmark, and editor-specific extension certification remain separate release gates. The RusaAi-Edge 1.54B manifest is still design-only; do not advertise inference as ready until a compatible model artifact is approved and the local chat/embedding smoke tests pass.
