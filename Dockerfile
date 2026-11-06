FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    RUSA_MAX_REQUEST_BYTES=1048576 \
    PYTHONPATH=/app:/app/llm-project/serve \
    PYTHONPATH=/app

WORKDIR /app

RUN groupadd --system --gid 10001 rusa \
    && useradd --system --uid 10001 --gid rusa --home-dir /nonexistent --shell /usr/sbin/nologin rusa \
    && mkdir -p /data \
    && chown -R 10001:10001 /data

COPY --from=builder /install /usr/local
COPY llm-project/serve /app/llm-project/serve
COPY rusa_agents /app/rusa_agents
COPY schemas /app/schemas

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)" || exit 1

FROM runtime AS agent-runtime
CMD ["python", "-m", "rusa_agents.worker"]

FROM runtime AS model-runtime
USER root
COPY requirements-model.txt /tmp/requirements-model.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements-model.txt \
    && rm -f /tmp/requirements-model.txt
USER 10001:10001
CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM runtime AS api
CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
