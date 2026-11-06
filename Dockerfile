FROM cgr.dev/chainguard/python:3.11-dev AS builder

USER root
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt \
    && /venv/bin/pip uninstall -y pip setuptools wheel jaraco.context \
    && rm -f /venv/bin/pip /venv/bin/pip3 /venv/bin/pip3.11

FROM cgr.dev/chainguard/python:3.11 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    RUSA_MAX_REQUEST_BYTES=1048576 \
    PATH="/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /venv /venv
COPY --chown=65532:65532 llm-project/serve /app/llm-project/serve
COPY --chown=65532:65532 schemas /app/schemas

USER 65532:65532

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

ENTRYPOINT ["/venv/bin/python"]
CMD ["-m", "uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
