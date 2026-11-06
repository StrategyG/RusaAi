import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from uuid import uuid4

import anyio
import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

if __package__:
    from .inference import BackendBusy, ContextLimit, Finished, TransformersBackend
else:
    from inference import BackendBusy, ContextLimit, Finished, TransformersBackend

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rusa-ai-serve")

CHAT_PATH = "/v1/chat/completions"


def load_model(backend, path):
    try:
        backend.load(path)
    except Exception:
        logger.error("Could not load the configured inference model")


@asynccontextmanager
async def lifespan(app):
    backend = app.state.backend = TransformersBackend()
    app.state.max_request_bytes = int(os.getenv("RUSA_MAX_REQUEST_BYTES", "1048576"))
    if app.state.max_request_bytes <= 0:
        raise ValueError("RUSA_MAX_REQUEST_BYTES must be positive")
    path = os.getenv("RUSA_MODEL_PATH", "").strip()
    loader = asyncio.create_task(asyncio.to_thread(load_model, backend, path)) if path else None
    try:
        yield
    finally:
        if loader is not None:
            await loader
        if backend.worker is not None:
            await backend.worker


class ChatBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != CHAT_PATH:
            await self.app(scope, receive, send)
            return

        limit = scope["app"].state.max_request_bytes
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    raise HTTPException(413, "Request body exceeds the configured limit")
            return message

        await self.app(scope, limited_receive, send)


app = FastAPI(
    lifespan=lifespan,
    title="RUSA-AI Serving Engine",
    description="Production-ready API for RUSA-AI model inference and task validation.",
    version="1.0.0"
)

app.add_middleware(ChatBodyLimit)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    expected_key = os.getenv("RUSA_API_KEY")
    if expected_key:
        if not api_key or api_key != expected_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API Key"
            )
    return api_key

class ChatMessage(BaseModel):
    role: str = Field(..., description="Message role: system, user, or assistant")
    content: str = Field(..., description="Content of the message")

class ChatCompletionRequest(BaseModel):
    model: str = Field(default="rusa-ai-v1", description="Model identifier")
    messages: List[ChatMessage] = Field(..., description="List of chat messages")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    stream: bool = Field(default=False)

class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]

class TaskValidationRequest(BaseModel):
    envelope: Dict[str, Any]

class TaskValidationResponse(BaseModel):
    valid: bool
    errors: Optional[List[str]] = None

@app.get("/health/live", status_code=status.HTTP_200_OK)
@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "production")
    }

@app.get("/health/ready")
def readiness(request: Request):
    if not request.app.state.backend.ready:
        raise HTTPException(503, "Inference model is not ready")
    return {"status": "ready"}


class ChatStream(StreamingResponse):
    def __init__(self, events, cancelled):
        super().__init__(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
        self.cancelled = cancelled

    async def __call__(self, scope, receive, send):
        try:
            async with anyio.create_task_group() as group:
                async def watch_disconnect():
                    await self.listen_for_disconnect(receive)
                    group.cancel_scope.cancel()

                group.start_soon(watch_disconnect)
                await self.stream_response(send)
                group.cancel_scope.cancel()
        finally:
            self.cancelled.set()
            await self.body_iterator.aclose()


def sse(data):
    return f"data: {json.dumps(data)}\n\n"


def inference_error(exc):
    if isinstance(exc, ContextLimit):
        return HTTPException(400, "Prompt must fit the context window with the requested output")
    if isinstance(exc, asyncio.TimeoutError):
        return HTTPException(504, "Inference timed out")
    logger.error("Inference backend failed")
    return HTTPException(502, "Inference backend failed")


async def first_stream_event(request, next_event):
    async def watch_disconnect():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    generation = asyncio.create_task(next_event())
    disconnect = asyncio.create_task(watch_disconnect())
    try:
        done, _ = await asyncio.wait({generation, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            raise HTTPException(499, "Client disconnected")
        return generation.result()
    finally:
        generation.cancel()
        disconnect.cancel()
        await asyncio.gather(generation, disconnect, return_exceptions=True)


async def chat_events(metadata, first, next_event):
    def chunk(delta, reason=None):
        return sse({
            **metadata, "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        })

    yield chunk({"role": "assistant"})
    event = first
    try:
        while not isinstance(event, Finished):
            yield chunk({"content": event})
            event = await next_event()
    except Exception as exc:
        error = inference_error(exc)
        code = "inference_timeout" if error.status_code == 504 else "backend_error"
        yield sse({"error": {"message": error.detail, "type": "server_error", "code": code}})
        return
    yield chunk({}, event.reason)
    yield "data: [DONE]\n\n"


@app.post(CHAT_PATH, response_model=ChatCompletionResponse, dependencies=[Depends(verify_api_key)])
async def chat_completions(payload: ChatCompletionRequest, request: Request):
    if not payload.messages:
        raise HTTPException(400, "Messages list cannot be empty")

    backend = request.app.state.backend
    if not backend.ready:
        raise HTTPException(503, "Inference model is not ready")
    if payload.model != backend.name:
        raise HTTPException(404, "Requested model is not available")

    try:
        next_event, cancelled = backend.start(
            [message.model_dump() for message in payload.messages],
            payload.temperature, payload.top_p, payload.max_tokens,
        )
    except BackendBusy:
        raise HTTPException(429, "Inference backend is busy") from None

    metadata = {"id": f"rusa-chatcmpl-{uuid4().hex}", "created": int(time.time()), "model": backend.name}
    stream_started = False
    try:
        if payload.stream:
            first = await first_stream_event(request, next_event)
            response = ChatStream(chat_events(metadata, first, next_event), cancelled)
            stream_started = True
            return response

        parts = []
        event = await next_event()
        while not isinstance(event, Finished):
            parts.append(event)
            event = await next_event()
        return ChatCompletionResponse(
            **metadata,
            choices=[ChatChoice(
                index=0, message=ChatMessage(role="assistant", content="".join(parts)),
                finish_reason=event.reason,
            )],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise inference_error(exc) from None
    finally:
        if not stream_started:
            cancelled.set()


@app.post("/v1/validate-task", response_model=TaskValidationResponse)
def validate_task_envelope(request: TaskValidationRequest):
    schema_path = os.path.join(os.path.dirname(__file__), "..", "..", "schemas", "a2a", "task-envelope.schema.json")
    if not os.path.exists(schema_path):
        schema_path = "schemas/a2a/task-envelope.schema.json"
    
    if not os.path.exists(schema_path):
        return TaskValidationResponse(valid=True, errors=["Schema file not found on disk, skipped validation."])

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        
        validator = jsonschema.Draft7Validator(schema)
        errors = [err.message for err in validator.iter_errors(request.envelope)]
        
        if errors:
            return TaskValidationResponse(valid=False, errors=errors)
        return TaskValidationResponse(valid=True, errors=[])
    except Exception as e:
        return TaskValidationResponse(valid=False, errors=[str(e)])
