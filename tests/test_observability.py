from fastapi import FastAPI
from fastapi.testclient import TestClient

from rusa_observability.metrics import (
    INFERENCE_REQUESTS,
    TOKEN_USAGE,
    RAG_EVAL_RECALL,
    RAG_EVAL_MRR,
    RAG_EVAL_NDCG,
    record_inference,
    record_retrieval_evaluation,
)
from rusa_observability.middleware import install_metrics


def test_inference_metrics_record_latency_outcome_and_exact_tokens():
    model = "test-observability-model"
    before_requests = INFERENCE_REQUESTS.labels(model, "success")._value.get()
    before_prompt = TOKEN_USAGE.labels(model, "prompt")._value.get()
    before_completion = TOKEN_USAGE.labels(model, "completion")._value.get()

    record_inference(
        model,
        "success",
        0.25,
        prompt_tokens=17,
        completion_tokens=9,
    )

    assert INFERENCE_REQUESTS.labels(model, "success")._value.get() == before_requests + 1
    assert TOKEN_USAGE.labels(model, "prompt")._value.get() == before_prompt + 17
    assert TOKEN_USAGE.labels(model, "completion")._value.get() == before_completion + 9


def test_metrics_endpoint_exposes_http_request_metrics():
    app = FastAPI()

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    install_metrics(app, "test-observability-api")
    client = TestClient(app)
    assert client.get("/health/live").status_code == 200
    response = client.get("/metrics")
    assert response.status_code == 200
    assert 'rusa_http_requests_total{method="GET",route="/health/live",service="test-observability-api",status_code="200"}' in response.text


def test_retrieval_evaluation_updates_quality_gauges():
    report = {
        "queries": 12,
        "k": 5,
        "recall_at_k": 0.91,
        "mrr_at_k": 0.82,
        "ndcg_at_k": 0.87,
    }
    record_retrieval_evaluation(report)

    assert RAG_EVAL_RECALL.labels("5")._value.get() == 0.91
    assert RAG_EVAL_MRR.labels("5")._value.get() == 0.82
    assert RAG_EVAL_NDCG.labels("5")._value.get() == 0.87
