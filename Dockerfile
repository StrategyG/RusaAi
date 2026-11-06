FROM python:3.12.16-slim-trixie AS builder

WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip "setuptools>=84.1.0" "wheel>=0.46.2" \
    && python -m pip install --no-cache-dir --prefix=/install -r requirements.txt
COPY pyproject.toml .
COPY rusa_agents ./rusa_agents
RUN python -m pip install --no-cache-dir --prefix=/install --no-deps .

# Start the final image from a clean Debian runtime. Copying the merged Python
# filesystem (rather than inheriting Python-image layers) prevents stale,
# vulnerable base-layer package metadata from surviving the final OCI image.
FROM debian:trixie-slim AS runner

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    LD_LIBRARY_PATH=/usr/local/lib
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libexpat1 libffi8 libssl3t64 libbz2-1.0 liblzma5 libsqlite3-0 libreadline8t64 libncursesw6 libgdbm6t64 libgdbm-compat4t64 zlib1g libuuid1 libcrypt1 libnsl2 libtirpc3t64 libatomic1 \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && echo /usr/local/lib > /etc/ld.so.conf.d/usr-local.conf \
    && ldconfig \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /usr/local /usr/local
COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . /app

RUN ldd /usr/local/bin/python3.12 | tee /tmp/python-ldd \
    && ! grep -q "not found" /tmp/python-ldd \
    && python -m pip check \
    && python - <<'PY'
from importlib.metadata import distributions

for dist in distributions():
    if (dist.metadata.get("Name") or "").lower() == "setuptools":
        version = tuple(int(part) for part in dist.version.split(".")[:3] if part.isdigit())
        if version < (84, 1, 0):
            raise SystemExit(f"Vulnerable setuptools {dist.version} remains at {dist._path}")
PY

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000"]
