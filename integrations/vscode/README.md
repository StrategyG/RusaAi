# RUSA AI Local — Visual Studio Code extension

This extension provides a minimal VS Code Chat Participant that sends the explicit user prompt to a local RUSA AI OpenAI-compatible endpoint.

## Connect

1. Start RUSA AI locally and open its first-run page.
2. Copy the API key and keep it private.
3. Install the packaged `.vsix` from the official RUSA AI release.
4. Run **RUSA AI: Configure Local API** from the Command Palette.
5. Set the local endpoint (default `http://127.0.0.1:8000/v1`) and paste the key.
6. Open VS Code Chat and select `@rusaai`.

The API key is stored in VS Code SecretStorage, not in workspace settings or source files. The extension sends only the prompt text explicitly entered into the chat participant; it does not automatically upload workspace files or terminal output.

## Security

- HTTPS is required for non-loopback endpoints.
- HTTP is accepted only for localhost/loopback.
- Redirects are not followed by the browser fetch.
- The key is never written to logs or settings.
- Tool execution and file edits are not implemented by this initial chat participant. Hybrid Agent workspace execution requires the separately reviewed Local Agent Bridge.

## Build

```bash
npm install
npm run compile
npm run package
```

The extension source is an integration foundation. A published VSIX, marketplace listing, automatic browser-to-editor pairing, streaming response support and end-to-end release tests remain pending.
