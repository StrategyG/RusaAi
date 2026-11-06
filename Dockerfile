FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/install \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    -r requirements.txt

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    RUSA_MAX_REQUEST_BYTES=1048576

WORKDIR /app

RUN groupadd --system --gid 10001 rusa \
    && useradd --system --uid 10001 --gid rusa --home-dir /nonexistent --shell /usr/sbin/nologin rusa \
    && mkdir -p /var/lib/rusa-agent \
    && chown -R 10001:10001 /var/lib/rusa-agent

COPY --from=builder /install /usr/local
COPY llm-project/serve /app/llm-project/serve
COPY schemas /app/schemas
COPY rusa_agents /app/rusa_agents
COPY rusa_knowledge /app/rusa_knowledge

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
