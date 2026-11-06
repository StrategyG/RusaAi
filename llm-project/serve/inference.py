"""Local, checkpoint-backed inference runtime for RUSA-AI.

No mock or synthetic completion path is provided. The service is ready only after
an operator mounts a real Hugging Face-compatible checkpoint and the runtime loads it.
"""
from __future__ import annotations

import asyncio
import os
import queue
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Sequence


class InferenceNotReady(RuntimeError):
    """Raised when a request arrives before a real model is loaded."""


_STREAM_END = object()


def _next_stream_item(streamer: Any) -> Any:
    try:
        return next(streamer)
    except queue.Empty:
        return None
    except StopIteration:
        return _STREAM_END


class ModelRuntime:
    """Loads a local causal language model and provides bounded inference methods."""

    def __init__(self) -> None:
        self.model_path = os.getenv("RUSA_MODEL_PATH", "").strip()
        self.model_name = os.getenv("RUSA_MODEL_NAME", "").strip()
        self.max_input_tokens = int(os.getenv("RUSA_MAX_INPUT_TOKENS", "8192"))
        self.model: Any = None
        self.tokenizer: Any = None
        self.device = "unloaded"
        self._torch: Any = None

    @property
    def ready(self) -> bool:
        return self.model is not None and self.tokenizer is not None

    def load(self) -> None:
        """Load only local model files; never download weights or execute remote code."""
        if not self.model_path:
            raise RuntimeError("RUSA_MODEL_PATH is not configured")

        checkpoint = Path(self.model_path).expanduser().resolve()
        if not checkpoint.is_dir():
            raise RuntimeError("RUSA_MODEL_PATH must point to a mounted model directory")

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Inference dependencies are missing; install requirements.txt"
            ) from exc

        if torch.cuda.is_available():
            dtype = (
                torch.bfloat16
                if getattr(torch.cuda, "is_bf16_supported", lambda: False)()
                else torch.float16
            )
            self.device = "cuda"
            torch.backends.cuda.matmul.allow_tf32 = True
        else:
            dtype = torch.float32
            self.device = "cpu"

        tokenizer = AutoTokenizer.from_pretrained(
            str(checkpoint),
            local_files_only=True,
            trust_remote_code=False,
            use_fast=True,
        )
        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is None:
                raise RuntimeError("Tokenizer must define an EOS or PAD token")
            tokenizer.pad_token = tokenizer.eos_token
        tokenizer.padding_side = "left"

        model = AutoModelForCausalLM.from_pretrained(
            str(checkpoint),
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=dtype,
            device_map="auto",
            low_cpu_mem_usage=True,
        )
        model.eval()

        self.model = model
        self.tokenizer = tokenizer
        if not self.model_name:
            self.model_name = checkpoint.name

    def _require_ready(self) -> None:
        if not self.ready:
            raise InferenceNotReady("The configured model is not loaded")

    def _prompt(self, messages: Sequence[Dict[str, str]]) -> str:
        self._require_ready()
        normalized = [{"role": item["role"], "content": item["content"]} for item in messages]
        if hasattr(self.tokenizer, "apply_chat_template"):
            try:
                return self.tokenizer.apply_chat_template(
                    normalized,
                    tokenize=False,
                    add_generation_prompt=True,
                )
            except (ValueError, TypeError, AttributeError):
                pass
        return "\n".join(
            f"{item['role'].upper()}: {item['content']}" for item in normalized
        ) + "\nASSISTANT:"

    def _tokenize_one(self, messages: Sequence[Dict[str, str]]) -> Dict[str, Any]:
        encoded = self.tokenizer(
            self._prompt(messages),
            return_tensors="pt",
            truncation=True,
            max_length=self.max_input_tokens,
        )
        return {key: value.to(self.model.device) for key, value in encoded.items()}

    def _generation_kwargs(self, temperature: float, top_p: float, max_tokens: int) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "max_new_tokens": max_tokens,
            "do_sample": temperature > 0,
            "top_p": top_p,
            "pad_token_id": self.tokenizer.pad_token_id,
            "eos_token_id": self.tokenizer.eos_token_id,
            "use_cache": True,
        }
        if temperature > 0:
            kwargs["temperature"] = temperature
        return kwargs

    def generate(
        self,
        messages: Sequence[Dict[str, str]],
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        self._require_ready()
        torch = self._torch or __import__("torch")
        inputs = self._tokenize_one(messages)
        prompt_tokens = int(inputs["attention_mask"].sum().item())
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                **self._generation_kwargs(temperature, top_p, max_tokens),
            )
        prompt_width = int(inputs["input_ids"].shape[1])
        generated_ids = output[0][prompt_width:]
        text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
        completion_tokens = int(generated_ids.shape[0])
        return {
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }

    def generate_batch(
        self,
        message_batches: Sequence[Sequence[Dict[str, str]]],
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> List[Dict[str, Any]]:
        """Run a true padded tensor batch with shared sampling parameters."""
        self._require_ready()
        if not message_batches:
            return []
        prompts = [self._prompt(messages) for messages in message_batches]
        inputs = self.tokenizer(
            prompts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_input_tokens,
        )
        prompt_counts = inputs["attention_mask"].sum(dim=1).tolist()
        inputs = {key: value.to(self.model.device) for key, value in inputs.items()}
        torch = self._torch or __import__("torch")
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                **self._generation_kwargs(temperature, top_p, max_tokens),
            )
        prompt_width = int(inputs["input_ids"].shape[1])
        results: List[Dict[str, Any]] = []
        for index, row in enumerate(output):
            generated_ids = row[prompt_width:]
            results.append(
                {
                    "text": self.tokenizer.decode(
                        generated_ids, skip_special_tokens=True
                    ),
                    "prompt_tokens": int(prompt_counts[index]),
                    "completion_tokens": int(generated_ids.shape[0]),
                }
            )
        return results

    async def stream(
        self,
        messages: Sequence[Dict[str, str]],
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream decoded text chunks from Transformers' generation thread."""
        self._require_ready()
        try:
            import torch
            from transformers import TextIteratorStreamer
        except ImportError as exc:
            raise RuntimeError("Streaming inference dependencies are unavailable") from exc

        inputs = self._tokenize_one(messages)
        streamer = TextIteratorStreamer(
            self.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            timeout=0.25,
        )
        kwargs = self._generation_kwargs(temperature, top_p, max_tokens)
        kwargs["streamer"] = streamer

        def run_generation() -> None:
            with torch.inference_mode():
                self.model.generate(**inputs, **kwargs)

        generation_task = asyncio.create_task(asyncio.to_thread(run_generation))
        try:
            while True:
                item = await asyncio.to_thread(_next_stream_item, streamer)
                if item is _STREAM_END:
                    break
                if item is None:
                    if generation_task.done():
                        await generation_task
                        break
                    continue
                if item:
                    yield item
            await generation_task
        finally:
            if not generation_task.done():
                # The Transformers streamer is not cancellable mid-kernel. Awaiting
                # completion avoids leaving an untracked GPU generation thread.
                await asyncio.gather(generation_task, return_exceptions=True)

    def close(self) -> None:
        self.model = None
        self.tokenizer = None
        if self.device == "cuda":
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
