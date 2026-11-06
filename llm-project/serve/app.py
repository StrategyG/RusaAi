import os
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rusa-ai-serve")

TASK_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/a2a/task-envelope.schema.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        schema = json.loads(TASK_SCHEMA_PATH.read_text(encoding="utf-8"))
        if not isinstance(schema, dict) or schema.get("$schema") != Draft202012Validator.META_SCHEMA["$id"]:
            raise ValueError("Expected JSON Schema Draft 2020-12")
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, SchemaError):
        raise RuntimeError("Unable to load a valid A2A task envelope schema") from None

    app.state.task_validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    yield


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

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {
        "status": "healthy",
        "service": "rusa-ai-serve",
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "production")
    }

@app.post("/v1/chat/completions", response_model=ChatCompletionResponse, dependencies=[Depends(verify_api_key)])
def chat_completions(request: ChatCompletionRequest):
    try:
        if not request.messages:
            raise HTTPException(status_code=400, detail="Messages list cannot be empty")
        
        last_msg = request.messages[-1].content
        mock_response = f"[RUSA-AI Model Output]: Processed prompt: '{last_msg}' successfully."
        
        import time
        return ChatCompletionResponse(
            id=f"rusa-chatcmpl-{int(time.time())}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=mock_response),
                    finish_reason="stop"
                )
            ]
        )
    except Exception as e:
        logger.error(f"Inference error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Inference engine failure: {str(e)}")

@app.post("/v1/validate-task", response_model=TaskValidationResponse)
def validate_task_envelope(payload: TaskValidationRequest, request: Request):
    errors = []
    for error in request.app.state.task_validator.iter_errors(payload.envelope):
        path = "/".join(str(part) for part in error.absolute_path)
        location = f"/{path}" if path else "$"
        if error.validator == "required":
            missing = [name for name in error.validator_value if name not in error.instance]
            message = f"missing required fields: {', '.join(missing)}"
        elif error.validator == "format":
            message = f"expected {error.validator_value} format"
        elif error.validator == "additionalProperties":
            message = "remove unsupported fields"
        else:
            message = f"failed {error.validator} validation"
        detail = f"{location}: {message}"
        if detail not in errors:
            errors.append(detail)
        if len(errors) == 10:
            break

    return TaskValidationResponse(valid=not errors, errors=errors)
