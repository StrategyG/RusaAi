# Serving and runtime module

This directory is reserved for:

- local inference runtime
- batching and caching
- quantization
- API server and health endpoints
- performance monitoring

Status: PLANNED


Security implementation: see ../../docs/SECURITY_HARDENING.md. Run locally with PYTHONPATH=llm-project/serve uvicorn app:app. Production startup requires a hashed API-key registry, PostgreSQL with the reviewed RLS migration, and Redis.
