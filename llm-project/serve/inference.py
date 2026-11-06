"""Checkpoint-backed Transformers inference with one active generation per process."""

import asyncio
import math
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event


class ContextLimit(ValueError):
    pass


class BackendBusy(RuntimeError):
    pass


@dataclass
class Finished:
    reason: str


class TransformersBackend:
    def __init__(self):
        self.name = os.getenv("RUSA_MODEL_NAME", "rusa-ai-v1")
        self.model = None
        self.worker = None
        self.timeout = float(os.getenv("RUSA_GENERATION_TIMEOUT_SECONDS", "120"))
        self.max_context = int(os.getenv("RUSA_MAX_MODEL_LEN", "8192"))
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.max_context <= 0:
            raise ValueError("Inference limits must be positive and finite")

    @property
    def ready(self):
        return self.model is not None

    def load(self, path):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        checkpoint = Path(path).expanduser().resolve()
        if not checkpoint.is_dir():
            raise ValueError("Model path must be a local checkpoint directory")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        options = {"local_files_only": True, "trust_remote_code": False}
        self.tokenizer = AutoTokenizer.from_pretrained(checkpoint, **options)
        model = AutoModelForCausalLM.from_pretrained(checkpoint, use_safetensors=True, **options)
        context = getattr(model.config, "max_position_embeddings", None)
        if isinstance(context, int) and context > 0:
            self.max_context = min(self.max_context, context)
        self.model = model.to(self.device).eval()

    def generate(self, messages, temperature, top_p, max_tokens, cancelled, emit):
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList, TextStreamer

        if self.tokenizer.chat_template:
            inputs = self.tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True,
                return_tensors="pt", return_dict=True,
            )
        else:
            prompt = "\n".join(
                f"{item['role'].upper()}: {item['content']}" for item in messages
            ) + "\nASSISTANT:"
            inputs = self.tokenizer(prompt, return_tensors="pt")

        prompt_tokens = inputs["input_ids"].shape[-1]
        if prompt_tokens == 0 or prompt_tokens + max_tokens > self.max_context:
            raise ContextLimit()
        if cancelled.is_set():
            return
        inputs = inputs.to(self.device)

        class StopOnCancellation(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return cancelled.is_set()

        class Streamer(TextStreamer):
            def on_finalized_text(self, text, stream_end=False):
                if text:
                    emit(text)

        sampling = {"temperature": temperature, "top_p": top_p} if temperature > 0 else {}
        with torch.inference_mode():
            output = self.model.generate(
                **inputs, max_new_tokens=max_tokens, do_sample=temperature > 0,
                pad_token_id=self.tokenizer.pad_token_id,
                streamer=Streamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True),
                stopping_criteria=StoppingCriteriaList([StopOnCancellation()]),
                num_beams=1, num_return_sequences=1,
                return_dict_in_generate=False, **sampling,
            )
        generated = output[0, prompt_tokens:]
        eos = self.model.generation_config.eos_token_id
        eos_ids = eos if isinstance(eos, list) else [eos]
        hit_eos = len(generated) > 0 and int(generated[-1]) in eos_ids
        emit(Finished("length" if len(generated) == max_tokens and not hit_eos else "stop"))

    def start(self, messages, temperature, top_p, max_tokens):
        if self.worker is not None and not self.worker.done():
            raise BackendBusy()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        cancelled = Event()
        events = asyncio.Queue()

        def emit(event):
            if not cancelled.is_set():
                loop.call_soon_threadsafe(events.put_nowait, event)

        def run():
            try:
                self.generate(messages, temperature, top_p, max_tokens, cancelled, emit)
            except Exception as exc:
                emit(exc)

        # A timed-out or disconnected request retains the slot until its thread exits.
        timer = loop.call_at(deadline, cancelled.set)
        self.worker = asyncio.create_task(asyncio.to_thread(run))
        worker = self.worker
        worker.add_done_callback(lambda worker: timer.cancel())

        async def next_event():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            event = await asyncio.wait_for(events.get(), remaining)
            if isinstance(event, Exception):
                raise event
            if isinstance(event, Finished):
                await asyncio.shield(worker)
            return event

        return next_event, cancelled
