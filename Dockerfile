FROM python:3.12.16-slim-trixie AS builder

WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY docker/build-patched-native-libs.sh /tmp/build-patched-native-libs.sh
RUN sh /tmp/build-patched-native-libs.sh
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip "setuptools>=84.1.0" "wheel>=0.46.2" \
    && python -m pip install --no-cache-dir --prefix=/install -r requirements.txt
COPY pyproject.toml .
COPY rusa_agents ./rusa_agents
ARG RUSA_INSTALL_LOCAL_MODEL=0
RUN python -m pip install --no-cache-dir --prefix=/install --no-deps . \
    && if [ "$RUSA_INSTALL_LOCAL_MODEL" = "1" ]; then \
         python -m pip install --no-cache-dir --prefix=/install -r requirements-model.txt; \
       fi
RUN PYTHONPATH=/install/lib/python3.12/site-packages python -m pip check \
    && python -m pip uninstall -y setuptools wheel jaraco.context \
    && python -m pip uninstall -y pip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

# Start the final image from a clean Debian runtime. Copying the merged Python
# filesystem (rather than inheriting Python-image layers) prevents stale,
# vulnerable base-layer package metadata from surviving the final OCI image.
FROM debian:trixie-slim AS runner

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    LD_LIBRARY_PATH=/usr/local/lib
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libffi8 libssl3t64 libbz2-1.0 liblzma5 libreadline8t64 libncursesw6 libgdbm6t64 libgdbm-compat4t64 zlib1g libuuid1 libcrypt1 libnsl2 libtirpc3t64 libatomic1 \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get purge -y libexpat1 libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/* \
    && echo /usr/local/lib > /etc/ld.so.conf.d/usr-local.conf \
    && ldconfig \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /usr/local /usr/local
COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . /app

RUN ldconfig && ldd /usr/local/bin/python3.12 | tee /tmp/python-ldd \
    && ! grep -q "not found" /tmp/python-ldd \
    && python - <<'PY'
from importlib import import_module, metadata

import pyexpat, sqlite3
if pyexpat.EXPAT_VERSION != "expat_2.10.0":
    raise SystemExit(f"Unpatched Expat runtime: {pyexpat.EXPAT_VERSION}")
if sqlite3.sqlite_version_info < (3, 54, 1):
    raise SystemExit(f"Unpatched SQLite runtime: {sqlite3.sqlite_version}")
print(f"Patched native libraries active: {pyexpat.EXPAT_VERSION}; SQLite {sqlite3.sqlite_version}")

for package in ("pip", "setuptools", "wheel", "jaraco.context"):
    try:
        metadata.version(package)
    except metadata.PackageNotFoundError:
        continue
    raise SystemExit(f"Build-only package {package} remains in the API runtime")

for module in (
    "fastapi", "uvicorn", "pydantic", "jsonschema",
    "httpx", "psycopg", "prometheus_client", "rusa_agents",
):
    import_module(module)
print("API runtime imports verified; build tooling is absent.")
PY

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000"]
