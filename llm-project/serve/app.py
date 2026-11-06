"""RUSA-AI OpenAI-compatible API with fail-closed security and real inference."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field

from inference import InferenceNotReady, ModelRuntime

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("rusa-ai-serve")

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "a2a"
    / "task-envelope.schema.json"
)
MAX_REQUEST_BYTES = int(os.getenv("RUSA_MAX_REQUEST_BYTES", str(1_048_576)))
MAX_BATCH_SIZE = int(os.getenv("RUSA_MAX_BATCH_SIZE", "16"))
MAX_TOKENS = int(os.getenv("RUSA_MAX_TOKENS", "4096"))
QUEUE_TIMEOUT_SECONDS = float(os.getenv("RUSA_QUEUE_TIMEOUT_SECONDS", "30"))


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Enforce a hard body limit, including chunked requests without Content-Length."""

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds configured limit"},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length"}
                )
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            response = JSONResponse(
                status_code=413,
                content={"detail": "Request body exceeds configured limit"},
            )
            await response(scope, receive, send)


class ProcessRateLimiter:
    """Best-effort per-process limiter; production ingress must also enforce shared limits."""

    def __init__(self) -> None:
        self._events: Dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] >= window_seconds:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True


rate_limiter = ProcessRateLimiter()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(StrictModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=200_000)


class StreamOptions(StrictModel):
    include_usage: bool = False


class ChatCompletionRequest(StrictModel):
    model: str = "rusa-ai"
    messages: List[ChatMessage] = Field(min_length=1, max_length=256)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1, le=MAX_TOKENS)
    stream: bool = False
    stream_options: Optional[StreamOptions] = None


class BatchChatItem(StrictModel):
    messages: List[ChatMessage] = Field(min_length=1, max_length=256)


class BatchChatCompletionRequest(StrictModel):
    model: str = "rusa-ai"
    requests: List[BatchChatItem] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    max_tokens: int = Field(default=512, ge=1, le=MAX_TOKENS)


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Literal["stop", "length"]


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: Usage


class TaskValidationRequest(StrictModel):
    envelope: Dict[str, Any]


class TaskValidationResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list)


def verify_api_key(api_key: Optional[str] = Security(api_key_header)) -> Optional[str]:
    expected_key = os.getenv("RUSA_API_KEY", "")
    environment = os.getenv("ENVIRONMENT", "development").lower()

    if not expected_key:
        if environment == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API authentication is not configured",
            )
        return None

    if not api_key or not hmac.compare_digest(api_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return api_key


@asynccontextmanager
async def lifespan(application: FastAPI):
    environment = os.getenv("ENVIRONMENT", "development").lower()
    api_key = os.getenv("RUSA_API_KEY", "")
    model_path = os.getenv("RUSA_MODEL_PATH", "").strip()

    if environment == "production":
        if len(api_key) < 32:
            raise RuntimeError("Production requires RUSA_API_KEY with at least 32 characters")
        if not model_path:
            raise RuntimeError("Production requires RUSA_MODEL_PATH to a real local checkpoint")

    runtime = ModelRuntime()
    application.state.runtime = runtime
    application.state.inference_semaphore = asyncio.Semaphore(
        max(1, int(os.getenv("RUSA_MAX_CONCURRENT_REQUESTS", "1")))
    )

    if model_path:
        await asyncio.to_thread(runtime.load)
        logger.info(
            "model_loaded model=%s device=%s backend=transformers",
            runtime.model_name,
            runtime.device,
        )
    else:
        logger.warning("model_not_loaded; inference endpoints will return 503")

    yield
    runtime.close()


app = FastAPI(
    title="RUSA-AI Serving Engine",
    description="Checkpoint-backed OpenAI-compatible inference and A2A validation API.",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)


@app.middleware("http")
async def request_controls(request: Request, call_next: Any):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id

    if request.url.path.startswith("/v1/"):
        limit = max(1, int(os.getenv("RUSA_RATE_LIMIT_PER_MINUTE", "120")))
        client_host = request.client.host if request.client else "unknown"
        if not rate_limiter.allow(client_host, limit):
            response = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
            response.headers["X-Request-ID"] = request_id
            return response

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


async def acquire_capacity(request: Request) -> asyncio.Semaphore:
    semaphore = request.app.state.inference_semaphore
    try:
        await asyncio.wait_for(
            semaphore.acquire(), timeout=QUEUE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="Inference capacity is busy; retry later",
            headers={"Retry-After": "5"},
        ) from exc
    return semaphore


def check_requested_model(requested: str, runtime: ModelRuntime) -> None:
    allowed = {runtime.model_name, "rusa-ai", "rusa-ai-v1"}
    if requested not in allowed:
        raise HTTPException(status_code=404, detail="Requested model is not available")


def make_completion(
    result: Dict[str, Any], model_name: str, request_id: str
) -> ChatCompletionResponse:
    completion_tokens = int(result["completion_tokens"])
    return ChatCompletionResponse(
        id=f"chatcmpl-{request_id.replace('-', '')}",
        created=int(time.time()),
        model=model_name,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content=result["text"] or " "),
                finish_reason="length" if completion_tokens >= MAX_TOKENS else "stop",
            )
        ],
        usage=Usage(
            prompt_tokens=int(result["prompt_tokens"]),
            completion_tokens=completion_tokens,
            total_tokens=int(result["prompt_tokens"]) + completion_tokens,
        ),
    )


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check(request: Request) -> Dict[str, Any]:
    runtime: ModelRuntime = request.app.state.runtime
    return {
        "status": "ok",
        "service": "rusa-ai-serve",
        "version": app.version,
        "model_ready": runtime.ready,
        "device": runtime.device,
    }


@app.get("/health/live", status_code=status.HTTP_200_OK)
def liveness() -> Dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def readiness(request: Request) -> Dict[str, Any]:
    runtime: ModelRuntime = request.app.state.runtime
    if not runtime.ready:
        raise HTTPException(
            status_code=503, detail="Model checkpoint is not loaded"
        )
    return {
        "status": "ready",
        "model": runtime.model_name,
        "device": runtime.device,
    }


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)],
)
async def chat_completions(
    payload: ChatCompletionRequest, request: Request
):
    runtime: ModelRuntime = request.app.state.runtime
    if not runtime.ready:
        raise HTTPException(status_code=503, detail="Inference model is not ready")
    check_requested_model(payload.model, runtime)

    semaphore = await acquire_capacity(request)
    messages = [item.model_dump() for item in payload.messages]
    request_id = request.state.request_id

    if payload.stream:
        async def event_stream():
            created = int(time.time())
            response_id = f"chatcmpl-{request_id.replace('-', '')}"
            try:
                yield "data: " + json.dumps({
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": runtime.model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }) + "\n\n"
                async for piece in runtime.stream(
                    messages,
                    payload.temperature,
                    payload.top_p,
                    payload.max_tokens,
                ):
                    yield "data: " + json.dumps({
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": runtime.model_name,
                        "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                    }, ensure_ascii=False) + "\n\n"
                yield "data: " + json.dumps({
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": runtime.model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }) + "\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                logger.error(
                    "stream_inference_failed request_id=%s error_type=%s",
                    request_id,
                    type(exc).__name__,
                )
                yield "data: " + json.dumps({
                    "error": {
                        "message": "Inference failed",
                        "type": "server_error",
                        "request_id": request_id,
                    }
                }) + "\n\n"
                yield "data: [DONE]\n\n"
            finally:
                semaphore.release()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        result = await asyncio.to_thread(
            runtime.generate,
            messages,
            payload.temperature,
            payload.top_p,
            payload.max_tokens,
        )
        return make_completion(result, runtime.model_name, request_id)
    except InferenceNotReady as exc:
        raise HTTPException(status_code=503, detail="Inference model is not ready") from exc
    except Exception as exc:
        logger.error(
            "inference_failed request_id=%s error_type=%s",
            request_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={"message": "Inference failed", "request_id": request_id},
        ) from exc
    finally:
        semaphore.release()


@app.post(
    "/v1/chat/completions/batch",
    dependencies=[Depends(verify_api_key)],
)
async def batch_chat_completions(
    payload: BatchChatCompletionRequest, request: Request
):
    """Non-streaming tensor-batched inference; all items share sampling settings."""
    runtime: ModelRuntime = request.app.state.runtime
    if not runtime.ready:
        raise HTTPException(status_code=503, detail="Inference model is not ready")
    check_requested_model(payload.model, runtime)

    semaphore = await acquire_capacity(request)
    message_batches = [
        [message.model_dump() for message in item.messages]
        for item in payload.requests
    ]
    try:
        results = await asyncio.to_thread(
            runtime.generate_batch,
            message_batches,
            payload.temperature,
            payload.top_p,
            payload.max_tokens,
        )
        created = int(time.time())
        data = []
        for index, result in enumerate(results):
            completion = make_completion(
                result, runtime.model_name, f"{request.state.request_id}-{index}"
            )
            data.append(completion.model_dump())
        return {"object": "list", "data": data, "created": created}
    except Exception as exc:
        logger.error(
            "batch_inference_failed request_id=%s error_type=%s",
            request.state.request_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Batch inference failed",
                "request_id": request.state.request_id,
            },
        ) from exc
    finally:
        semaphore.release()


@app.post(
    "/v1/validate-task",
    response_model=TaskValidationResponse,
    dependencies=[Depends(verify_api_key)],
)
def validate_task_envelope(request: TaskValidationRequest):
    if not SCHEMA_PATH.is_file():
        raise HTTPException(
            status_code=503,
            detail="A2A validation schema is unavailable; validation cannot be skipped",
        )

    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema, format_checker=FormatChecker()
        )
        errors = sorted(
            validator.iter_errors(request.envelope),
            key=lambda error: list(error.absolute_path),
        )
        return TaskValidationResponse(
            valid=not errors,
            errors=[
                f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
                for error in errors
            ],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("schema_validation_failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="A2A validation is temporarily unavailable",
        ) from exc
