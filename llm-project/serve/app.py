import json
import logging
import os
import time
from contextlib import asynccontextmanager
from itertools import islice
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema
from fastapi import Depends, FastAPI, HTTPException, Request, status
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
MAX_VALIDATION_ERRORS = 50


def load_task_envelope_validator() -> Draft202012Validator:
    """Load and validate the A2A schema; fail closed on any startup problem."""
    try:
        with SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)

        if not isinstance(schema, dict):
            raise ValueError("The A2A schema root must be a JSON object.")
        expected_draft = Draft202012Validator.META_SCHEMA["$id"]
        if schema.get("$schema") != expected_draft:
            raise ValueError("The A2A schema must declare JSON Schema Draft 2020-12.")

        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())
    except (OSError, json.JSONDecodeError, ValueError, jsonschema.SchemaError) as exc:
        raise RuntimeError(
            f"Unable to load valid A2A task envelope schema at {SCHEMA_PATH}"
        ) from exc


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.task_envelope_validator = load_task_envelope_validator()
    yield
    application.state.task_envelope_validator = None


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
    errors: List[str] = Field(default_factory=list)


def _json_path(error: jsonschema.ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{str(part)[:80]}"
    return path[:256]


def _client_validation_message(error: jsonschema.ValidationError) -> str:
    """Return actionable field-level feedback without echoing submitted values."""
    path = _json_path(error)

    if error.validator == "required" and isinstance(error.instance, dict):
        missing = next(
            (
                field
                for field in error.validator_value
                if field not in error.instance
                and error.message == f"{field!r} is a required property"
            ),
            None,
        )
        if missing is None:
            missing_fields = sorted(set(error.validator_value) - set(error.instance))
            missing = missing_fields[0] if missing_fields else "field"
        return f"{path}.{missing}: is required"

    if error.validator == "type":
        return f"{path}: must have type {error.validator_value}"
    if error.validator == "format":
        return f"{path}: must use the {error.validator_value} format"
    if error.validator == "enum":
        allowed = ", ".join(str(value) for value in error.validator_value)
        return f"{path}: must be one of [{allowed}]"
    if error.validator == "const":
        return f"{path}: must equal the required value"
    if error.validator == "minLength":
        return f"{path}: must contain at least {error.validator_value} characters"
    if error.validator == "maxLength":
        return f"{path}: must contain at most {error.validator_value} characters"
    if error.validator == "minItems":
        return f"{path}: must contain at least {error.validator_value} items"
    if error.validator == "pattern":
        return f"{path}: does not match the required pattern"
    if error.validator == "additionalProperties":
        if isinstance(error.instance, dict):
            allowed_fields = error.schema.get("properties", {})
            unsupported = sorted(set(error.instance) - set(allowed_fields))
            if unsupported:
                names = ", ".join(str(field)[:80] for field in unsupported[:10])
                remainder = len(unsupported) - 10
                if remainder > 0:
                    names += f", and {remainder} more"
                return f"{path}: unsupported field(s): {names}"
        return f"{path}: contains an unsupported field"
    return f"{path}: does not satisfy the schema"


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
def validate_task_envelope(
    request: TaskValidationRequest,
    http_request: Request,
):
    validator = getattr(http_request.app.state, "task_envelope_validator", None)
    if validator is None:
        logger.error("A2A schema validator is not initialized")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task validation is unavailable",
        )

    errors = list(islice(validator.iter_errors(request.envelope), MAX_VALIDATION_ERRORS + 1))
    has_more_errors = len(errors) > MAX_VALIDATION_ERRORS
    errors = sorted(
        errors[:MAX_VALIDATION_ERRORS],
        key=lambda error: (
            list(map(str, error.absolute_path)),
            error.validator or "",
            error.message,
        ),
    )
    client_errors = [_client_validation_message(error) for error in errors]
    if has_more_errors:
        client_errors.append(
            f"$: more than {MAX_VALIDATION_ERRORS} validation errors; "
            "correct the listed issues and resubmit"
        )

    return TaskValidationResponse(valid=not client_errors, errors=client_errors)
