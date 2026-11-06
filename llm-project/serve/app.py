import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("rusa-ai-serve")

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "a2a"
    / "task-envelope.schema.json"
)
TASK_ENVELOPE_VALIDATOR: Optional[Draft202012Validator] = None


def load_task_envelope_validator() -> Draft202012Validator:
    """Load and validate the A2A schema; fail closed on any startup problem."""
    try:
        with SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise RuntimeError(
            f"Unable to load valid A2A task envelope schema at {SCHEMA_PATH}"
        ) from exc


@asynccontextmanager
async def lifespan(_: FastAPI):
    global TASK_ENVELOPE_VALIDATOR
    TASK_ENVELOPE_VALIDATOR = load_task_envelope_validator()
    yield
    TASK_ENVELOPE_VALIDATOR = None


app = FastAPI(
    title="RUSA-AI Serving Engine",
    description="Production-ready API for RUSA-AI model inference and task validation.",
    version="1.0.0",
    lifespan=lifespan,
)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    expected_key = os.getenv("RUSA_API_KEY")
    if expected_key and (not api_key or api_key != expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
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


def _client_validation_message(error: jsonschema.ValidationError) -> str:
    """Return actionable field-level feedback without echoing submitted values."""
    path = ".".join(str(part) for part in error.absolute_path) or "$"
    messages = {
        "required": "is required",
        "additionalProperties": "contains an unsupported field",
        "type": "has an invalid type",
        "format": "has an invalid format",
        "const": "does not match the required value",
        "enum": "is not an allowed value",
        "minLength": "is shorter than allowed",
        "maxLength": "is longer than allowed",
        "minItems": "contains too few items",
        "pattern": "has an invalid format",
    }
    return f"{path}: {messages.get(error.validator, 'is invalid')}"


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "production"),
    }


@app.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(verify_api_key)],
)
def chat_completions(request: ChatCompletionRequest):
    try:
        if not request.messages:
            raise HTTPException(status_code=400, detail="Messages list cannot be empty")

        last_msg = request.messages[-1].content
        mock_response = (
            f"[RUSA-AI Model Output]: Processed prompt: '{last_msg}' successfully."
        )
        now = int(time.time())
        return ChatCompletionResponse(
            id=f"rusa-chatcmpl-{now}",
            created=now,
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=mock_response),
                    finish_reason="stop",
                )
            ],
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Inference error")
        raise HTTPException(
            status_code=500, detail="Inference engine failure"
        ) from None


@app.post("/v1/validate-task", response_model=TaskValidationResponse)
def validate_task_envelope(request: TaskValidationRequest):
    if TASK_ENVELOPE_VALIDATOR is None:
        logger.error("A2A schema validator is not initialized")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task validation is unavailable",
        )

    errors = sorted(
        TASK_ENVELOPE_VALIDATOR.iter_errors(request.envelope),
        key=lambda error: (list(map(str, error.absolute_path)), error.validator or ""),
    )
    return TaskValidationResponse(
        valid=not errors,
        errors=[_client_validation_message(error) for error in errors],
    )
