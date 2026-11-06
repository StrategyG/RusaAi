FROM python:3.12-slim AS builder

WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip install --upgrade pip "setuptools>=78.1.1" "wheel>=0.46.2" \
    && python -m pip install --prefix=/install -r requirements.txt
COPY pyproject.toml .
COPY rusa_agents ./rusa_agents
RUN python -m pip install --prefix=/install --no-deps .

FROM python:3.12-slim AS runner

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . /app

# Upgrade inherited Python packaging tools after all application dependencies
# have been copied. The final image must contain only patched package versions.
RUN python -m pip install --no-cache-dir --upgrade \
      pip "setuptools>=78.1.1" "wheel>=0.46.2" "jaraco.context>=6.1.0" \
    && python -m pip check \
    && python - <<'PY'
from importlib.metadata import version
from packaging.version import Version

minimums = {"setuptools": "78.1.1", "wheel": "0.46.2", "jaraco.context": "6.1.0"}
for package, minimum in minimums.items():
    installed = version(package)
    if Version(installed) < Version(minimum):
        raise SystemExit(f"{package} {installed} is below required {minimum}")
    print(f"{package}=={installed}")
PY

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000"]
