"""Authenticated, lease-fenced sandbox executor isolated from coding workers."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from .tool_gateway import DockerSandbox, ToolDenied

logger = logging.getLogger("rusa-ai.sandbox-executor")


class SandboxRunRequest(BaseModel):
    """The only operation accepted by the executor; no path or command is caller-controlled."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    task_id: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: UUID
    profile: Literal["pytest", "compile", "ruff", "bandit"]


class PostgresSandboxLeaseVerifier:
    """Read-only lease verifier; runtime login can execute one narrowly scoped SQL function."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn:
            raise ValueError("Sandbox executor database DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use sandbox lease verification") from exc
        return psycopg.connect(self.dsn, connect_timeout=5)

    def verify_ready(self) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT current_user, role.rolsuper, role.rolbypassrls, role.rolcanlogin,
                       pg_has_role(current_user, 'rusa_sandbox_runtime', 'member'),
                       has_function_privilege(
                           current_user,
                           'public.rusa_verify_sandbox_lease(uuid,text,text,uuid)',
                           'EXECUTE'
                       ),
                       has_table_privilege(current_user, 'public.aicode_jobs', 'SELECT')
                FROM pg_roles AS role
                WHERE role.rolname = current_user
                """
            ).fetchone()
        if (
            not row
            or row[0] != "rusa_sandbox_login"
            or row[1]
            or row[2]
            or not row[3]
            or not row[4]
            or not row[5]
            or row[6]
        ):
            raise RuntimeError(
                "Sandbox DB identity must be rusa_sandbox_login with function-only, read-only access"
            )

    def verify_lease(
        self, task_id: UUID, tenant_id: str, worker_id: str, lease_token: UUID
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT public.rusa_verify_sandbox_lease(
                    %s::uuid, %s, %s, %s::uuid
                )
                """,
                (str(task_id), tenant_id, worker_id, str(lease_token)),
            ).fetchone()
        return bool(row and row[0])


def _workspace_for_lease(
    workspace_root: Path, tenant_id: str, task_id: UUID, lease_token: UUID
) -> Path:
    tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:20]
    attempt_key = hashlib.sha256(str(lease_token).encode("utf-8")).hexdigest()[:12]
    return workspace_root / tenant_key / str(task_id) / attempt_key


def _validate_workspace(workspace_root: Path, workspace: Path) -> Path:
    """Reject symlinked components and paths outside the executor's read-only workspace mount."""
    current = workspace_root
    relative = workspace.relative_to(workspace_root)
    for part in relative.parts:
        current = current / part
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            raise ToolDenied("Sandbox workspace contains a symbolic-link component")
    resolved = workspace.resolve(strict=True)
    if resolved != workspace or workspace_root not in resolved.parents or not resolved.is_dir():
        raise ToolDenied("Sandbox workspace is not a real directory under the assigned workspace root")
    return resolved


def _production_database_dsn_errors(dsn: str) -> list[str]:
    parsed = urlparse(dsn)
    errors: list[str] = []
    if parsed.scheme not in {"postgres", "postgresql"}:
        errors.append("RUSA_SANDBOX_EXECUTOR_DATABASE_URL must use PostgreSQL")
    if parsed.username != "rusa_sandbox_login":
        errors.append("Sandbox executor database login must be rusa_sandbox_login")
    if not parsed.hostname or not parsed.password:
        errors.append("Sandbox executor database DSN must include a host and password")
    if any(
        marker in (parsed.hostname or "").lower()
        or marker in (parsed.password or "").lower()
        for marker in ("placeholder", "redacted", "example", "db_host", "secret")
    ):
        errors.append("Sandbox executor database DSN must not contain placeholder host/password values")
    if parse_qs(parsed.query).get("sslmode", [""])[-1] != "verify-full":
        errors.append("Sandbox executor database DSN must use sslmode=verify-full")
    root_cert = parse_qs(parsed.query).get("sslrootcert", [""])[-1]
    if (
        not root_cert
        or not Path(root_cert).is_absolute()
        or not Path(root_cert).is_file()
    ):
        errors.append("Sandbox executor database DSN must reference a mounted sslrootcert file")
    return errors


def create_app(
    *,
    sandbox: DockerSandbox | None = None,
    verifier: PostgresSandboxLeaseVerifier | None = None,
    workspace_root: str | Path | None = None,
    token: str | None = None,
    require_rootless: bool | None = None,
    require_image: bool | None = None,
    max_concurrency: int | None = None,
) -> FastAPI:
    environment = os.getenv("ENVIRONMENT", "development").lower()
    production = environment == "production"
    image = os.getenv("RUSA_SANDBOX_IMAGE")
    configured_root = workspace_root or os.getenv("RUSA_SANDBOX_WORKSPACE_ROOT")
    snapshot_root = os.getenv("RUSA_SANDBOX_SNAPSHOT_ROOT")
    database_dsn = os.getenv("RUSA_SANDBOX_EXECUTOR_DATABASE_URL")
    secret = token if token is not None else os.getenv("RUSA_SANDBOX_EXECUTOR_TOKEN")
    if not configured_root:
        raise RuntimeError("RUSA_SANDBOX_WORKSPACE_ROOT is required")
    if not snapshot_root:
        raise RuntimeError("RUSA_SANDBOX_SNAPSHOT_ROOT is required")
    if not database_dsn and verifier is None:
        raise RuntimeError("RUSA_SANDBOX_EXECUTOR_DATABASE_URL is required")
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise RuntimeError("RUSA_SANDBOX_EXECUTOR_TOKEN must contain at least 32 UTF-8 bytes")
    if production:
        if not image or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/:@-]+@sha256:[0-9a-f]{64}", image
        ):
            raise RuntimeError("Production RUSA_SANDBOX_IMAGE must be pinned by an immutable sha256 digest")
        errors = _production_database_dsn_errors(database_dsn or "")
        if errors and verifier is None:
            raise RuntimeError("; ".join(errors))
    root = Path(configured_root).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("Sandbox workspace root must be an existing directory")
    snapshots = Path(snapshot_root).resolve()
    snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
    if production and (
        snapshots == root
        or root in snapshots.parents
        or snapshots in root.parents
    ):
        raise RuntimeError("Sandbox snapshot root must be separate from and outside the workspace root")
    if production:
        try:
            docker_gid = int(os.getenv("RUSA_DOCKER_GID", ""))
            snapshot_metadata = snapshots.stat()
        except (TypeError, ValueError, OSError):
            raise RuntimeError("RUSA_DOCKER_GID and the mounted snapshot directory are required") from None
        snapshot_mode = stat.S_IMODE(snapshot_metadata.st_mode)
        if (
            snapshot_metadata.st_gid != docker_gid
            or not snapshot_metadata.st_mode & stat.S_ISGID
            or snapshot_mode & 0o070 != 0o070
        ):
            raise RuntimeError(
                "Sandbox snapshot root must be setgid and group-writable/traversable by the rootless Docker GID"
            )
    if "," in str(root) or "," in str(snapshots):
        raise RuntimeError("Sandbox workspace and snapshot paths must not contain commas")

    sandbox = sandbox or DockerSandbox(image=image, snapshot_root=snapshots)
    verifier = verifier or PostgresSandboxLeaseVerifier(database_dsn or "")
    concurrency = (
        max_concurrency
        if max_concurrency is not None
        else int(os.getenv("RUSA_SANDBOX_MAX_CONCURRENCY", "2"))
    )
    if not 1 <= concurrency <= 16:
        raise RuntimeError("RUSA_SANDBOX_MAX_CONCURRENCY must be between 1 and 16")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(
            sandbox.verify_ready,
            require_rootless=production if require_rootless is None else require_rootless,
            require_image=production if require_image is None else require_image,
        )
        await asyncio.to_thread(verifier.verify_ready)
        app.state.ready = True
        yield
        app.state.ready = False

    app = FastAPI(
        title="RUSA-AI Sandbox Executor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.ready = False
    app.state.semaphore = asyncio.Semaphore(concurrency)

    @app.get("/healthz")
    async def healthz():
        if not app.state.ready:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not ready")
        return {
            "ready": True,
            "rootless": bool(getattr(sandbox, "rootless", False)),
            "image": sandbox.image,
        }

    @app.post("/v1/run")
    async def run_profile(
        request: SandboxRunRequest,
        authorization: str | None = Header(default=None),
    ):
        scheme, separator, supplied = (authorization or "").partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8"))
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        if not app.state.ready:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not ready")
        if not await asyncio.to_thread(
            verifier.verify_lease,
            request.task_id,
            request.tenant_id,
            request.worker_id,
            request.lease_token,
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="task lease is not active")
        workspace = _workspace_for_lease(
            root, request.tenant_id, request.task_id, request.lease_token
        )
        try:
            workspace = _validate_workspace(root, workspace)
        except (OSError, ValueError, ToolDenied):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task workspace unavailable") from None
        async with app.state.semaphore:
            result = await asyncio.to_thread(sandbox.run, workspace, request.profile)
        return {
            "ok": result.ok,
            "output": result.output[-20000:],
            "evidence": result.evidence or {},
        }

    return app


def main() -> None:
    import uvicorn

    host = os.getenv("RUSA_SANDBOX_EXECUTOR_BIND", "127.0.0.1")
    port = int(os.getenv("RUSA_SANDBOX_EXECUTOR_PORT", "9101"))
    cert = os.getenv("RUSA_SANDBOX_EXECUTOR_TLS_CERT_FILE")
    key = os.getenv("RUSA_SANDBOX_EXECUTOR_TLS_KEY_FILE")
    if os.getenv("ENVIRONMENT", "development").lower() == "production" and not (cert and key):
        raise RuntimeError("Production sandbox executor requires an HTTPS certificate and private key")
    application = create_app()
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_level="info",
        ssl_certfile=cert,
        ssl_keyfile=key,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
