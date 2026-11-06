FROM python:3.12-slim AS builder

WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip install --upgrade pip 'setuptools>=78.1.1' \
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

# Build tooling is not required at runtime. Remove it (including stale metadata
# copied from the builder) to keep the final image free of the setuptools CVE.
RUN python -m pip uninstall -y setuptools wheel jaraco.context || true
RUN SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && rm -rf "${SITE_PACKAGES}/setuptools" "${SITE_PACKAGES}"/setuptools-*.dist-info \
         "${SITE_PACKAGES}/wheel" "${SITE_PACKAGES}"/wheel-*.dist-info \
         "${SITE_PACKAGES}/jaraco/context" "${SITE_PACKAGES}"/jaraco_context-*.dist-info \
    && python -m pip check \
    && python - <<'PY'
from importlib.metadata import PackageNotFoundError, version

for package in ("setuptools", "wheel", "jaraco.context"):
    try:
        installed = version(package)
    except PackageNotFoundError:
        continue
    raise SystemExit(f"Build-only package {package} ({installed}) must not ship in runtime")
PY

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000"]
