FROM python:3.10-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements-serve.txt .
RUN python -m pip install --prefix=/install -r requirements-serve.txt

FROM python:3.10-slim AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --uid 10001 --create-home appuser

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser llm-project/serve ./llm-project/serve
COPY --chown=appuser:appuser schemas ./schemas

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--app-dir", "llm-project/serve", "--host", "0.0.0.0", "--port", "8000"]
