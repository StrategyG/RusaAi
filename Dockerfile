FROM python:3.10-slim AS builder

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip install --prefix=/install -r requirements.txt

FROM python:3.10-slim AS runner

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    RUSA_MODEL_PATH=/models/rusa-ai \
    TOKENIZERS_PARALLELISM=false \
    PYTHONFAULTHANDLER=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /models/rusa-ai \
    && chown -R appuser:appuser /models

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . /app

USER appuser
VOLUME ["/models"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)" || exit 1

# One worker per model process prevents duplicate checkpoint loads and GPU overcommit.
CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30"]
