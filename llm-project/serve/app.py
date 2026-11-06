"""RUSA-AI API with fail-closed authentication, RBAC, RLS and bounded requests."""
from __future__ import annotations
import asyncio, json, logging, os, time, uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field

from database import TenantDatabase
from security import Principal, require_permission, load_api_key_registry

logger = logging.getLogger("rusa-ai-serve")
A2A_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "a2a" / "task-envelope.schema.json"
RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""

class MemoryRateLimiter:
    """Development/test-only limiter. Production requires shared Redis."""
    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def check(self, request: Request, principal: Principal) -> None:
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            raise HTTPException(status_code=503, detail="Rate limiting service is unavailable")
        key = f"{principal.tenant_id}:{principal.subject}"
        now = time.monotonic()
        async with self.lock:
            events = self.events[key]
            while events and events[0] <= now - self.window:
                events.popleft()
            if len(events) >= self.limit:
                raise HTTPException(status_code=429, detail="Rate limit exceeded",
                                    headers={"Retry-After": str(self.window)})
            events.append(now)

class RateLimiter:
    def __init__(self, limit: int, window: int):
        if limit < 1 or window < 1:
            raise ValueError("Rate limit and window must be positive integers")
        self.limit, self.window = limit, window
        self.local = MemoryRateLimiter(limit, window)

    async def check(self, request: Request, principal: Principal) -> None:
        redis_client = request.app.state.redis
        if redis_client is None:
            await self.local.check(request, principal)
            return
        import hashlib
        identity = hashlib.sha256(f"{principal.tenant_id}:{principal.subject}".encode()).hexdigest()
        try:
            count = await redis_client.eval(RATE_LIMIT_SCRIPT, 1, f"rusa:ratelimit:{identity}", self.window)
        except Exception as exc:
            logger.error("rate_limit_backend_error", extra={"error_type": type(exc).__name__})
            raise HTTPException(status_code=503, detail="Rate limiting service is unavailable") from None
        if int(count) > self.limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded",
                                headers={"Retry-After": str(self.window)})

class RequestSizeLimitMiddleware:
    """Bound request bodies, including chunked requests without Content-Length."""
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared:
            try:
                size = int(declared)
                if size < 0:
                    await self._respond(send, 400, "Invalid Content-Length")
                    return
                if size > self.max_bytes:
                    await self._respond(send, 413, "Request body too large")
                    return
            except ValueError:
                await self._respond(send, 400, "Invalid Content-Length")
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await self._respond(send, 413, "Request body too large")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        sent = False
        async def replay_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}
        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _respond(send, code: int, detail: str):
        payload = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)

class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="rusa-ai-v1", min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    stream: bool = False

class TaskValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope: dict[str, Any]

class TaskValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)

class RecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, Any]

def _serialize_record(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row["created_at"]
    return {"id": str(row["id"]), "data": row["payload"],
            "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at)}

def create_app(*, max_request_bytes: int | None = None) -> FastAPI:
    production = os.getenv("ENVIRONMENT", "development").lower() == "production"

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = redis_client = None
        if production:
            try:
                if not load_api_key_registry():
                    raise ValueError("API key registry is empty")
            except ValueError:
                raise RuntimeError("Production authentication configuration is invalid") from None
            if not os.getenv("RUSA_DATABASE_URL"):
                raise RuntimeError("Production PostgreSQL configuration is required")
            if not os.getenv("RUSA_REDIS_URL"):
                raise RuntimeError("Production Redis configuration is required")
        if os.getenv("RUSA_DATABASE_URL"):
            try:
                database = TenantDatabase(os.environ["RUSA_DATABASE_URL"])
                database.open()
            except Exception as exc:
                logger.error("database_startup_failed", extra={"error_type": type(exc).__name__})
                raise RuntimeError("PostgreSQL security preflight failed") from None
        if os.getenv("RUSA_REDIS_URL"):
            try:
                from redis.asyncio import Redis
                redis_client = Redis.from_url(os.environ["RUSA_REDIS_URL"], socket_connect_timeout=3,
                                              socket_timeout=3, decode_responses=True)
                await redis_client.ping()
            except Exception as exc:
                logger.error("redis_startup_failed", extra={"error_type": type(exc).__name__})
                if database:
                    database.close()
                raise RuntimeError("Redis rate-limit preflight failed") from None
        application.state.database = database
        application.state.redis = redis_client
        application.state.rate_limiter = RateLimiter(
            int(os.getenv("RUSA_RATE_LIMIT_MAX_REQUESTS", "60")),
            int(os.getenv("RUSA_RATE_LIMIT_WINDOW_SECONDS", "60")))
        try:
            yield
        finally:
            if redis_client:
                await redis_client.aclose()
            if database:
                database.close()

    application = FastAPI(
        title="RUSA-AI Serving Engine",
        description="Authenticated RUSA-AI API with tenant-scoped authorization.",
        version="1.1.0",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
        lifespan=lifespan)
    request_limit = max_request_bytes if max_request_bytes is not None else int(os.getenv("RUSA_MAX_REQUEST_BYTES", "1048576"))
    if request_limit < 1:
        raise ValueError("RUSA_MAX_REQUEST_BYTES must be a positive integer")
    application.add_middleware(RequestSizeLimitMiddleware, max_bytes=request_limit)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        issues = [{"location": [str(part) for part in error.get("loc", ())],
                   "type": str(error.get("type", "validation_error"))} for error in exc.errors()]
        return JSONResponse(status_code=422,
                            content={"detail": "Request validation failed", "errors": issues})

    @application.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.error("unhandled_request_error", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @application.get("/health", status_code=status.HTTP_200_OK)
    def health_check():
        return {"status": "ok"}

    @application.get("/ready", status_code=status.HTTP_200_OK)
    def readiness_check(request: Request):
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            if request.app.state.database is None or request.app.state.redis is None:
                raise HTTPException(status_code=503, detail="Service dependencies are not ready")
            raise HTTPException(status_code=503, detail="Inference backend is not configured")
        return {"status": "ready"}

    @application.post("/v1/chat/completions")
    def chat_completions(request: ChatCompletionRequest,
                         principal: Principal = Depends(require_permission("chat:invoke"))):
        if request.stream:
            raise HTTPException(status_code=501, detail="Streaming is not implemented by this serving adapter")
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            raise HTTPException(status_code=503, detail="Inference backend is not configured")
        last_message = request.messages[-1].content
        return {"id": f"rusa-chatcmpl-{uuid.uuid4()}", "object": "chat.completion",
                "created": int(time.time()), "model": request.model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": f"[development mock] {last_message}"},
                             "finish_reason": "stop"}]}

    @application.post("/v1/validate-task", response_model=TaskValidationResponse)
    def validate_task_envelope(request: TaskValidationRequest,
                               principal: Principal = Depends(require_permission("task:validate"))):
        try:
            if not A2A_SCHEMA_PATH.is_file():
                raise FileNotFoundError
            with A2A_SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
                schema = json.load(schema_file)
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            errors = sorted(validator.iter_errors(request.envelope),
                            key=lambda error: (list(map(str, error.absolute_path)), str(error.validator)))
            safe_errors = []
            for error in errors:
                path = "/".join(str(part) for part in error.absolute_path) or "$"
                safe_errors.append(f"{path}: {error.validator} validation failed")
            return TaskValidationResponse(valid=not safe_errors, errors=safe_errors)
        except FileNotFoundError:
            logger.error("a2a_schema_missing")
            raise HTTPException(status_code=503, detail="A2A schema is unavailable") from None
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("a2a_schema_validation_unavailable", extra={"error_type": type(exc).__name__})
            raise HTTPException(status_code=503, detail="A2A validation is unavailable") from None

    @application.post("/v1/records", status_code=201)
    def create_record(body: RecordCreate, request: Request,
                      principal: Principal = Depends(require_permission("records:write"))):
        database = request.app.state.database
        if database is None:
            raise HTTPException(status_code=503, detail="Tenant database is unavailable")
        try:
            row = database.create_record(principal.tenant_id, principal.subject, body.data)
            return _serialize_record(row)
        except Exception as exc:
            logger.error("tenant_record_write_failed", extra={"error_type": type(exc).__name__})
            raise HTTPException(status_code=500, detail="Record operation failed") from None

    @application.get("/v1/records")
    def list_records(request: Request,
                     principal: Principal = Depends(require_permission("records:read"))):
        database = request.app.state.database
        if database is None:
            raise HTTPException(status_code=503, detail="Tenant database is unavailable")
        try:
            return {"items": [_serialize_record(row) for row in database.list_records(principal.tenant_id)]}
        except Exception as exc:
            logger.error("tenant_record_read_failed", extra={"error_type": type(exc).__name__})
            raise HTTPException(status_code=500, detail="Record operation failed") from None

    @application.get("/v1/records/{record_id}")
    def get_record(record_id: uuid.UUID, request: Request,
                   principal: Principal = Depends(require_permission("records:read"))):
        database = request.app.state.database
        if database is None:
            raise HTTPException(status_code=503, detail="Tenant database is unavailable")
        try:
            row = database.get_record(principal.tenant_id, str(record_id))
        except Exception as exc:
            logger.error("tenant_record_read_failed", extra={"error_type": type(exc).__name__})
            raise HTTPException(status_code=500, detail="Record operation failed") from None
        if row is None:
            raise HTTPException(status_code=404, detail="Record not found")
        return _serialize_record(row)

    return application

app = create_app()
