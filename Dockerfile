FROM python:3.11-slim AS builder

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim AS runner
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/llm-project/serve ENVIRONMENT=production

# Refresh runtime OS packages so the image receives available Debian security fixes.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# The base Python image may ship vulnerable packaging tools even when the app
# requirements do not depend on them. Keep the runtime toolchain above known fixes.
RUN python -m pip install --no-cache-dir --upgrade \
      pip setuptools "wheel>=0.46.2" "jaraco.context>=6.1.0" \
    && python -m pip check

RUN useradd -m -u 10001 appuser
COPY --from=builder /install /usr/local
COPY . .
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
