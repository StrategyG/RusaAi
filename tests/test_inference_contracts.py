import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "shared" / "contracts" / "inference_contract"


def load_schema(name):
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def valid_request():
    return {
        "schema_version": "1.0",
        "request_id": "REQ-smoke-001",
        "task_id": "TASK-smoke-001",
        "agent_id": "supreme_orchestrator",
        "model_id": "rusaai-edge-1.54b",
        "capability_profile": "ASTRA",
        "reasoning_mode": "STANDARD",
        "context": {
            "system_context": [],
            "project_context": [],
            "retrieved_evidence": [],
        },
        "generation": {"max_output_tokens": 1024},
        "constraints": {"deadline_ms": 30000, "gpu_offload": False},
    }


def valid_response():
    return {
        "schema_version": "1.0",
        "request_id": "REQ-smoke-001",
        "task_id": "TASK-smoke-001",
        "model_id": "rusaai-edge-1.54b",
        "status": "COMPLETED",
        "output": {"message": "ok"},
        "evidence": [],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        "performance": {
            "ttft_ms": 125.0,
            "generation_tokens_per_second": 8.5,
            "peak_ram_mb": 2048.0,
        },
        "verification": {"schema_valid": True, "orm_status": "PENDING"},
    }


def test_v1_inference_request_contract_accepts_cpu_request():
    jsonschema.validate(valid_request(), load_schema("inference-request.v1.schema.json"))


def test_v1_inference_request_rejects_gpu_offload_and_wrong_model():
    schema = load_schema("inference-request.v1.schema.json")
    request = valid_request()
    request["constraints"]["gpu_offload"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(request, schema)

    request = valid_request()
    request["model_id"] = "qwen3-4b"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(request, schema)


def test_v1_inference_response_contract_accepts_completed_result():
    jsonschema.validate(valid_response(), load_schema("inference-response.v1.schema.json"))


def test_v1_inference_response_rejects_unknown_fields():
    schema = load_schema("inference-response.v1.schema.json")
    response = valid_response()
    response["authorization_granted"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(response, schema)
