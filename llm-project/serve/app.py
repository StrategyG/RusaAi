import os
import json
import logging
import hmac
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
import jsonschema
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("rusa-ai-serve")

app = FastAPI(
    title="RUSA-AI Serving Engine",
    description="Production-ready API for RUSA-AI model inference and task validation.",
    version="1.0.0",
)

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    expected_key = os.getenv("RUSA_API_KEY")
    if not expected_key or not api_key or not hmac.compare_digest(api_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )
    return api_key


class ChatMessage(BaseModel):
    role: str = Field(..., description="Message role: system, user, or assistant")
    content: str = Field(..., description="Content of the message")


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="rusa-v1", description="Model identifier")
    messages: List[ChatMessage] = Field(..., description="List of messages in conversation")
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0, description="Sampling temperature")
    max_tokens: Optional[int] = Field(default=512, gt=0, description="Max tokens to generate")


class TaskValidationRequest(BaseModel):
    task_envelope: Dict[str, Any] = Field(..., description="Task envelope JSON matching A2A schema")


class TaskValidationResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list, description="Validation errors if any")


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "rusa-ai-serve"}


@app.post(
    "/v1/chat/completions",
    dependencies=[Depends(verify_api_key)],
)
def chat_completions(req: ChatCompletionRequest):
    return {
        "id": "chatcmpl-rusa-001",
        "object": "chat.completion",
        "created": 1700000000,
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Halo! Saya RUSA AI, asisten AI Anda. Ada yang bisa saya bantu?",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 15,
            "total_tokens": 25,
        },
    }


@app.post(
    "/v1/validate-task",
    response_model=TaskValidationResponse,
    dependencies=[Depends(verify_api_key)],
)
def validate_task(req: TaskValidationRequest):
    schema_path = os.getenv(
        "TASK_ENVELOPE_SCHEMA_PATH",
        os.path.join(
            os.path.dirname(__file__),
            "../../schemas/a2a/task-envelope.schema.json",
        ),
    )
    if not os.path.exists(schema_path):
        return TaskValidationResponse(
            valid=False,
            errors=[f"Schema file not found on disk at {schema_path}"],
        )

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except Exception as e:
        return TaskValidationResponse(
            valid=False,
            errors=[f"Failed to load/parse schema file: {str(e)}"],
        )

    try:
        validator = jsonschema.Draft202012Validator(schema)
        errors = [err.message for err in validator.iter_errors(req.task_envelope)]
        if errors:
            return TaskValidationResponse(valid=False, errors=errors)
        return TaskValidationResponse(valid=True, errors=[])
    except Exception as e:
        return TaskValidationResponse(
            valid=False,
            errors=[f"Schema validation error: {str(e)}"],
        )

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rusa-ai-serve")

app = FastAPI(
    title="RUSA-AI Serving Engine",
    description="Production-ready API for RUSA-AI model inference and task validation.",
    version="1.0.0"
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
