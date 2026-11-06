FROM python:3.13-slim-trixie AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM gcr.io/distroless/python3-debian13:nonroot AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    ENVIRONMENT=production \
    RUSA_MAX_REQUEST_BYTES=1048576 \
    PYTHONPATH=/usr/local/lib/python3.13/site-packages

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=65532:65532 llm-project/serve /app/llm-project/serve
COPY --chown=65532:65532 schemas /app/schemas

USER 65532:65532

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
