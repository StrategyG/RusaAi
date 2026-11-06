FROM python:3.13-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    ENVIRONMENT=production \
    RUSA_MAX_REQUEST_BYTES=1048576

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && for package in python3-msgpack python3-urllib3 python3-setuptools; do \
         if dpkg-query -s "$package" 2>/dev/null | grep -q '^Status: install ok installed

COPY --from=builder /install /usr/local
COPY --chown=10001:10001 llm-project/serve /app/llm-project/serve
COPY --chown=10001:10001 schemas /app/schemas

# Validate the final dependency set, then remove the package installer and wheel tooling.
RUN python -m pip check \
    && python -m pip uninstall -y pip wheel

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
; then \
           apt-get purge -y "$package"; \
         fi; \
       done \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 rusa \
    && useradd --system --uid 10001 --gid rusa --home-dir /nonexistent --shell /usr/sbin/nologin rusa

COPY --from=builder /install /usr/local
COPY --chown=10001:10001 llm-project/serve /app/llm-project/serve
COPY --chown=10001:10001 schemas /app/schemas

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

ENTRYPOINT ["python"]
CMD ["-m", "uvicorn", "app:app", "--app-dir", "/app/llm-project/serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
