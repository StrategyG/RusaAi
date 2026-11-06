import asyncio
import json
import sys
import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "llm-project" / "serve"))

from app import app
from inference import ContextLimit, Finished


CHAT = "/v1/chat/completions"
PAYLOAD = {"messages": [{"role": "user", "content": "probe"}], "max_tokens": 2}
HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.delenv("RUSA_MODEL_PATH", raising=False)
    monkeypatch.setenv("RUSA_MODEL_NAME", "rusa-ai-v1")
    monkeypatch.setenv("RUSA_API_KEY", "test-key")
    monkeypatch.setenv("RUSA_GENERATION_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("RUSA_MAX_REQUEST_BYTES", "256")


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def set_generator(client, generate):
    backend = client.app.state.backend
    backend.model = object()
    backend.generate = generate
    return backend


def complete(messages, temperature, top_p, max_tokens, cancelled, emit):
    emit("backend output")
    emit(Finished("stop"))


def events(response):
    return [line.removeprefix("data: ") for line in response.text.splitlines() if line]


def test_liveness_separate_from_readiness(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503
    assert client.post(CHAT, json=PAYLOAD, headers=HEADERS).status_code == 503
    set_generator(client, complete)
    assert client.get("/health/ready").json() == {"status": "ready"}
    assert client.post(CHAT, json={**PAYLOAD, "model": "missing"}, headers=HEADERS).status_code == 404
    assert client.post(CHAT, json=PAYLOAD).status_code == 401


def test_completion_and_sse(client):
    set_generator(client, complete)
    result = client.post(CHAT, json=PAYLOAD, headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["choices"][0]["message"]["content"] == "backend output"
    response = client.post(CHAT, json={**PAYLOAD, "stream": True}, headers=HEADERS)
    assert response.headers["content-type"].startswith("text/event-stream")
    role, content, final, done = events(response)
    role, content, final = map(json.loads, (role, content, final))
    assert role["id"] == content["id"] == final["id"]
    assert role["choices"][0]["delta"] == {"role": "assistant"}
    assert content["choices"][0]["delta"] == {"content": "backend output"}
    assert final["choices"][0]["finish_reason"] == "stop"
    assert done == "[DONE]"


@pytest.mark.parametrize("stream", [False, True])
def test_timeout_before_first_output(client, stream):
    stopped = Event()

    def stall(messages, temperature, top_p, max_tokens, cancelled, emit):
        if cancelled.wait(2):
            stopped.set()

    backend = set_generator(client, stall)
    backend.timeout = 0.1
    response = client.post(CHAT, json={**PAYLOAD, "stream": stream}, headers=HEADERS)
    assert response.status_code == 504
    assert stopped.wait(1)


def test_stream_timeout_after_content(client):
    def stall(messages, temperature, top_p, max_tokens, cancelled, emit):
        emit("partial")
        cancelled.wait(2)

    backend = set_generator(client, stall)
    backend.timeout = 0.3
    response = client.post(CHAT, json={**PAYLOAD, "stream": True}, headers=HEADERS)
    assert response.status_code == 200
    assert json.loads(events(response)[-1])["error"]["code"] == "inference_timeout"


def test_request_and_context_limits(client):
    def context_error(*args):
        raise ContextLimit()

    set_generator(client, context_error)
    body = json.dumps({**PAYLOAD, "padding": "x" * 300}).encode()
    assert client.post(CHAT, content=iter([body[:150], body[150:]]), headers={
        **HEADERS, "Content-Type": "application/json",
    }).status_code == 413
    assert client.post(CHAT, json={**PAYLOAD, "max_tokens": 4097}, headers=HEADERS).status_code == 422
    assert client.post(CHAT, json={**PAYLOAD, "stream": True}, headers=HEADERS).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("after_content", [False, True])
async def test_disconnect_cancels_generation(after_content):
    async with app.router.lifespan_context(app):
        backend = app.state.backend
        backend.model = object()
        backend.timeout = 10
        started = asyncio.Event()
        stopped = Event()
        disconnected = asyncio.Event()
        loop = asyncio.get_running_loop()
        sent = []

        def generate(messages, temperature, top_p, max_tokens, cancelled, emit):
            loop.call_soon_threadsafe(started.set)
            if after_content:
                emit("partial")
            if cancelled.wait(5):
                stopped.set()

        backend.generate = generate
        body = json.dumps({**PAYLOAD, "stream": True}).encode()
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await started.wait()
            if after_content:
                await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if b"partial" in message.get("body", b""):
                disconnected.set()

        await asyncio.wait_for(app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "path": CHAT, "scheme": "http",
            "query_string": b"", "headers": [(b"content-type", b"application/json"), (b"x-api-key", b"test-key")],
        }, receive, send), timeout=2)
        assert await asyncio.to_thread(stopped.wait, 1)
        if after_content:
            assert not any(b"[DONE]" in message.get("body", b"") for message in sent)


def test_real_transformers_checkpoint(tmp_path, monkeypatch):
    import torch
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import GPT2Config, GPT2LMHeadModel, PreTrainedTokenizerFast

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setenv("RUSA_MODEL_PATH", str(tmp_path))
    monkeypatch.setenv("RUSA_GENERATION_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("RUSA_MAX_MODEL_LEN", "32")
    previous_threads = torch.get_num_threads()
    torch.set_num_threads(1)
    try:
        torch.manual_seed(42)
        vocab = {"[UNK]": 0, "[PAD]": 1, "probe": 2, "verified": 3}
        raw_tokenizer = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
        raw_tokenizer.pre_tokenizer = Whitespace()
        tokenizer = PreTrainedTokenizerFast(
            tokenizer_object=raw_tokenizer, unk_token="[UNK]", pad_token="[PAD]",
            chat_template="{{ messages[-1]['content'] }}",
        )
        tokenizer.save_pretrained(tmp_path)
        config = GPT2Config(
            vocab_size=len(vocab), n_positions=32, n_embd=16, n_layer=1, n_head=1,
            bos_token_id=None, eos_token_id=None, pad_token_id=1,
        )
        model = GPT2LMHeadModel(config)
        optimizer = torch.optim.AdamW(model.parameters(), lr=0.02)
        example = torch.tensor([[2, 3, 3]])
        for _ in range(40):
            optimizer.zero_grad()
            model(example, labels=example).loss.backward()
            optimizer.step()
        model.eval()
        model.save_pretrained(tmp_path)
        prompt = torch.tensor([[2]])
        expected = tokenizer.decode(model.generate(prompt, max_new_tokens=2)[0, 1:])
        assert expected == "verified verified"

        payload = {**PAYLOAD, "temperature": 0}
        with TestClient(app) as client:
            deadline = time.monotonic() + 20
            while client.get("/health/ready").status_code != 200:
                assert time.monotonic() < deadline, "Local checkpoint did not become ready"
                time.sleep(0.02)
            response = client.post(CHAT, json=payload, headers=HEADERS)
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == expected
            stream = client.post(CHAT, json={**payload, "stream": True}, headers=HEADERS)
            chunks = events(stream)
            assert chunks[-1] == "[DONE]"
            deltas = [json.loads(chunk)["choices"][0]["delta"] for chunk in chunks[:-1]]
            assert "".join(delta.get("content", "") for delta in deltas) == expected
            print(f"CPU GPT-2 checkpoint, direct generation = JSON = SSE: {expected!r}")
    finally:
        torch.set_num_threads(previous_threads)
