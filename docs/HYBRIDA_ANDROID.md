# HYBRIDA AI — Android Local Edition

HYBRIDA is an Android-first, local-first edition of RUSA-AI. It reuses the repository's product direction but does not require the production Docker/PostgreSQL stack on a phone.

## What is included

- **Local model:** Qwen3 0.6B in GGUF Q4_0, served by llama.cpp on the phone CPU.
- **Local chat:** OpenAI-compatible streaming requests to http://127.0.0.1:8081/v1.
- **Local RAG:** SQLite documents and chunks, FTS5 full-text ranking, local TF-IDF sparse-vector ranking, and Reciprocal Rank Fusion. This avoids downloading a second embedding model and keeps the first Android build small. It is sparse lexical/vector retrieval, not dense neural embeddings.
- **Local MCP:** MCP Streamable HTTP endpoint at http://127.0.0.1:8765/mcp. Tools include search knowledge, list/read indexed documents, create a note, local time, and a bounded calculator.
- **Chat UI:** responsive, installable PWA with conversation history, streamed answers, document upload, RAG toggle, and a local tools panel.
- **Local persistence:** SQLite database in ~/.rusa-hybrida/hybrida.sqlite3; model files are kept outside the Git repository.

The local MCP tool set intentionally has no shell execution, arbitrary file browsing, or network-fetch tool. Documents are treated as untrusted evidence in the RAG prompt.

## Device profile

The default profile targets an ARM64 Android phone with 8 GB RAM and no root access:

| Setting | Default |
|---|---|
| Model | Qwen3 0.6B GGUF Q4_0 |
| Model artifact | about 429 MB |
| Runtime | llama.cpp CPU |
| Context | 2,048 tokens |
| Parallel requests | 1 |
| Generation limit | 512 tokens |
| RAG database | SQLite in app data |
| API bind | 127.0.0.1 only |

The small model is a resource-conscious baseline, not a quality equivalent to a larger cloud model. Generation speed depends on the phone's SoC, thermal state, available memory, and Android background restrictions. The PWA shell can be cached, but the local model and API processes must be running to generate answers.

## Install on Termux (no root)

Run these commands from the repository root in Termux:

    pkg update -y
    pkg install -y git
    git clone https://github.com/StrategyG/RusaAi.git
    cd RusaAi
    bash scripts/setup_hybrida_termux.sh
    bash scripts/download_hybrida_model.sh

The setup script installs the Python dependencies and builds the llama-server binary from the upstream llama.cpp source. The model download is separate because the model is large and is not committed to Git.

### Start the model

In Termux session 1:

    cd ~/RusaAi
    bash scripts/start_hybrida_model.sh

Wait until llama.cpp reports that the server is listening on port 8081.

### Start HYBRIDA

In Termux session 2:

    cd ~/RusaAi
    bash scripts/start_hybrida.sh

Open http://127.0.0.1:8765 in Chrome on the same phone. Use Chrome's browser menu and choose Add to Home screen or Install app to place HYBRIDA on the launcher.

If Termux is stopped or Android kills its background process, reopen both services. A Termux wake lock can help during long sessions, but it increases battery use.

## Local API

| Route | Purpose |
|---|---|
| GET /api/status | Model, RAG, and MCP readiness |
| GET /api/chats | List local conversations |
| POST /api/chats | Create a conversation |
| GET /api/chats/{id} | Read conversation history |
| POST /api/chat | Stream a Qwen response as server-sent events |
| GET /api/documents | List indexed documents |
| POST /api/documents | Ingest TXT, MD, HTML, PDF, or DOCX (10 MiB max) |
| GET /api/rag/search?q=... | Search local knowledge |
| GET /api/mcp/tools | List local tools |
| POST /api/mcp/call | Invoke a local tool from the UI |
| /mcp | MCP Streamable HTTP transport |

The HTTP service rejects non-loopback Host headers and cross-origin browser requests. It must remain bound to 127.0.0.1; do not expose port 8765 or 8081 to a LAN, reverse proxy, or public interface.

## Configuration

The scripts default to:

- RUSA_MOBILE_DATA_DIR=~/.rusa-hybrida
- RUSA_LLAMA_BASE_URL=http://127.0.0.1:8081/v1
- RUSA_LLAMA_MODEL=qwen-local

Override the model endpoint only with another HTTP loopback endpoint. The app rejects non-loopback model URLs. Keep downloaded model files in the Termux home directory rather than shared Android storage for better file access performance.

## Current scope and next engineering gates

This is a first runnable Android-local slice, not a production-certified Android APK. The UI is an installable PWA hosted by the local Termux service; a signed native APK/Capacitor wrapper is a later packaging step. RAG uses local sparse TF-IDF vectors plus FTS5; a separate dense embedding model is intentionally deferred to conserve RAM. The existing tenant-scoped PostgreSQL RAG/MCP service and the production AICode stack remain unchanged.

Before wider release, run on-device smoke tests for the target phone, check the llama.cpp build against its CPU features, profile memory/thermal throttling, test large PDF/DOCX parsing, and verify the MCP transport with the intended MCP client.
