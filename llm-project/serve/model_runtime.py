from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Protocol

logger = logging.getLogger("rusa-ai-runtime")
MANIFEST_NAME = "rusa-model-manifest.json"
_REQUIRED_TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "spiece.model",
    "vocab.json",
)
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")


class ModelArtifactError(RuntimeError):
    """Raised when a model artifact cannot be trusted or loaded."""


class TransientInferenceError(RuntimeError):
    """A retryable backend error explicitly classified as transient."""


@dataclass(frozen=True)
class VerifiedModelArtifact:
    root: Path
    manifest_path: Path
    manifest_sha256: str
    model_id: str
    revision: str
    license: str
    tokenizer_id: str
    tokenizer_revision: str
    files_verified: int


@dataclass(frozen=True)
class GenerationResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: Literal["stop", "length"]


@dataclass(frozen=True)
class GenerationEvent:
    delta: str = ""
    finish_reason: Literal["stop", "length"] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class InferenceBackend(Protocol):
    name: str
    device: str
    supports_batching: bool

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> GenerationResult: ...

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[GenerationEvent]: ...

    async def close(self) -> None: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_artifact(
    model_path: str | Path,
    *,
    expected_manifest_sha256: str | None,
    require_pinned_manifest: bool = True,
) -> VerifiedModelArtifact:
    root = Path(model_path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ModelArtifactError("RUSA_MODEL_PATH must point to a model directory")

    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ModelArtifactError(f"Required model manifest {MANIFEST_NAME} is missing")

    if require_pinned_manifest and not expected_manifest_sha256:
        raise ModelArtifactError(
            "RUSA_MODEL_MANIFEST_SHA256 is required when ENVIRONMENT=production"
        )

    manifest_sha = sha256_file(manifest_path)
    if expected_manifest_sha256:
        expected = expected_manifest_sha256.strip().lower()
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ModelArtifactError("RUSA_MODEL_MANIFEST_SHA256 must be a SHA-256 hex digest")
        if not hmac.compare_digest(manifest_sha, expected):
            raise ModelArtifactError("Model manifest SHA-256 does not match the pinned digest")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelArtifactError("Model manifest is unreadable or invalid JSON") from exc

    required_fields = (
        "schema_version",
        "model_id",
        "revision",
        "license",
        "tokenizer_id",
        "tokenizer_revision",
        "files",
    )
    if not isinstance(manifest, dict) or any(field not in manifest for field in required_fields):
        raise ModelArtifactError("Model manifest is missing required metadata")
    if manifest["schema_version"] != 1:
        raise ModelArtifactError("Unsupported model manifest schema_version")
    for field in required_fields[1:6]:
        if not isinstance(manifest[field], str) or not manifest[field].strip():
            raise ModelArtifactError(f"Model manifest field {field} must be a non-empty string")

    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ModelArtifactError("Model manifest files must be a non-empty path-to-SHA256 mapping")

    verified: set[str] = set()
    for relative_name, expected_digest in files.items():
        if not isinstance(relative_name, str) or not relative_name:
            raise ModelArtifactError("Model manifest contains an invalid file path")
        relative_path = Path(relative_name)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ModelArtifactError("Model manifest contains a path traversal entry")
        target = root / relative_path
        try:
            resolved_target = target.resolve(strict=True)
        except OSError as exc:
            raise ModelArtifactError(f"Manifest file is missing: {relative_name}") from exc
        if root not in resolved_target.parents or target.is_symlink() or not resolved_target.is_file():
            raise ModelArtifactError(f"Manifest file is not a regular file inside the model directory: {relative_name}")
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_digest.lower())
        ):
            raise ModelArtifactError(f"Invalid SHA-256 digest for {relative_name}")
        actual_digest = sha256_file(resolved_target)
        if not hmac.compare_digest(actual_digest, expected_digest.lower()):
            raise ModelArtifactError(f"Model artifact checksum mismatch: {relative_name}")
        verified.add(relative_path.as_posix())

    # The loader may inspect any file in the directory. Reject unmanifested files
    # and symlinks so an extra checkpoint shard cannot bypass the pinned manifest.
    for candidate in root.rglob("*"):
        if candidate == manifest_path or ".cache" in candidate.relative_to(root).parts:
            continue
        if candidate.is_symlink():
            raise ModelArtifactError("Model directories must not contain symlinks")
        if candidate.is_file():
            relative_name = candidate.relative_to(root).as_posix()
            if relative_name not in verified:
                raise ModelArtifactError(f"Unmanifested model artifact file: {relative_name}")

    if "config.json" not in verified:
        raise ModelArtifactError("Model artifact must contain a checksummed config.json")
    if not any(name in verified for name in _REQUIRED_TOKENIZER_FILES):
        raise ModelArtifactError("Model artifact does not contain a supported tokenizer file")
    if not any(name.lower().endswith(_WEIGHT_SUFFIXES) for name in verified):
        raise ModelArtifactError("Model artifact does not contain supported model weight files")

    return VerifiedModelArtifact(
        root=root,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        model_id=manifest["model_id"],
        revision=manifest["revision"],
        license=manifest["license"],
        tokenizer_id=manifest["tokenizer_id"],
        tokenizer_revision=manifest["tokenizer_revision"],
        files_verified=len(verified),
    )


def _generation_kwargs(max_new_tokens: int, temperature: float, top_p: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"max_new_tokens": max_new_tokens}
    if temperature == 0:
        kwargs["do_sample"] = False
    else:
        kwargs.update({"do_sample": True, "temperature": temperature, "top_p": top_p})
    return kwargs


class TransformersBackend:
    """Local Transformers baseline; generation is serialized per model process."""

    name = "transformers"
    supports_batching = False

    def __init__(self, model: Any, tokenizer: Any, torch: Any, device: str, timeout_seconds: float, max_model_len: int):
        self.model = model
        self.tokenizer = tokenizer
        self.torch = torch
        self.device = device
        self.timeout_seconds = timeout_seconds
        self.max_model_len = max_model_len
        self._generation_lock = threading.Lock()

    @classmethod
    def from_pretrained(cls, model_path: str, *, timeout_seconds: float, max_model_len: int) -> "TransformersBackend":
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        path = Path(model_path).resolve(strict=True)
        tokenizer = AutoTokenizer.from_pretrained(
            str(path),
            local_files_only=True,
            trust_remote_code=False,
        )
        if not getattr(tokenizer, "chat_template", None):
            raise ModelArtifactError("The configured tokenizer must provide a chat_template")
        if tokenizer.eos_token_id is None:
            raise ModelArtifactError("The configured tokenizer must define eos_token_id")
        if tokenizer.pad_token_id is None:
            tokenizer.pad_token = tokenizer.eos_token

        use_cuda = torch.cuda.is_available()
        device = "cuda" if use_cuda else "cpu"
        dtype = (
            torch.bfloat16
            if use_cuda and torch.cuda.is_bf16_supported()
            else torch.float16
            if use_cuda
            else torch.float32
        )
        load_args: dict[str, Any] = {
            "local_files_only": True,
            "trust_remote_code": False,
            "torch_dtype": dtype,
            "low_cpu_mem_usage": True,
        }
        if use_cuda:
            try:
                import accelerate  # noqa: F401
            except ImportError as exc:
                raise ModelArtifactError(
                    "CUDA model loading requires the accelerate package"
                ) from exc
            load_args["device_map"] = "auto"
            load_args["attn_implementation"] = "sdpa"

        model = AutoModelForCausalLM.from_pretrained(str(path), **load_args)
        if not use_cuda:
            model.to("cpu")
        model.eval()
        input_device = str(model.get_input_embeddings().weight.device)
        logger.info("Transformers model loaded device=%s dtype=%s", input_device, dtype)
        return cls(
            model=model,
            tokenizer=tokenizer,
            torch=torch,
            device=input_device,
            timeout_seconds=timeout_seconds,
            max_model_len=max_model_len,
        )

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> GenerationResult:
        chunks: list[str] = []
        result: GenerationResult | None = None
        async for event in self.stream(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            if event.delta:
                chunks.append(event.delta)
            if event.finish_reason:
                result = GenerationResult(
                    content="".join(chunks).strip(),
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    finish_reason=event.finish_reason,
                )
        if result is None or not result.content:
            raise RuntimeError("Transformers generation did not produce a completion")
        return result

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[GenerationEvent]:
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

        encoded = await asyncio.to_thread(
            self.tokenizer.apply_chat_template,
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        )
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        prompt_tokens = int(encoded["input_ids"].shape[-1])
        if prompt_tokens + max_new_tokens > self.max_model_len:
            raise ValueError(
                f"Prompt plus requested output exceeds the configured context limit ({self.max_model_len} tokens)"
            )
        streamer = TextIteratorStreamer(
            self.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            timeout=self.timeout_seconds,
        )
        cancel_event = threading.Event()
        result_holder: dict[str, Any] = {}
        error_holder: list[BaseException] = []
        eos_ids = self.tokenizer.eos_token_id
        pad_id = self.tokenizer.pad_token_id

        class StopOnCancellation(StoppingCriteria):
            def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> bool:
                return cancel_event.is_set()

        def run_generation() -> None:
            try:
                with self._generation_lock:
                    with self.torch.inference_mode():
                        output = self.model.generate(
                            **encoded,
                            streamer=streamer,
                            pad_token_id=pad_id,
                            eos_token_id=eos_ids,
                            stopping_criteria=StoppingCriteriaList([StopOnCancellation()]),
                            **_generation_kwargs(max_new_tokens, temperature, top_p),
                        )
                generated = output[0, prompt_tokens:]
                token_count = int(generated.shape[-1])
                last_token = int(generated[-1]) if token_count else None
                eos_set = set(eos_ids if isinstance(eos_ids, list) else [eos_ids])
                finish_reason: Literal["stop", "length"] = (
                    "stop"
                    if last_token in eos_set or token_count < max_new_tokens
                    else "length"
                )
                result_holder["event"] = GenerationEvent(
                    finish_reason=finish_reason,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=token_count,
                )
            except BaseException as exc:  # surfaced in the async request task
                error_holder.append(exc)
                try:
                    streamer.on_finalized_text("", stream_end=True)
                except Exception:
                    pass

        worker = threading.Thread(target=run_generation, name="rusa-transformers-generation", daemon=True)
        worker.start()
        sentinel = object()

        def next_text() -> object | str:
            try:
                return next(streamer)
            except StopIteration:
                return sentinel

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        asyncio.to_thread(next_text),
                        timeout=self.timeout_seconds,
                    )
                except (asyncio.TimeoutError, queue.Empty) as exc:
                    raise TimeoutError("Timed out waiting for the next generated token") from exc
                if item is sentinel:
                    break
                if isinstance(item, str) and item:
                    yield GenerationEvent(delta=item)

            await asyncio.to_thread(worker.join, self.timeout_seconds)
            if worker.is_alive():
                cancel_event.set()
                raise TimeoutError("Transformers generation did not stop before its deadline")
            if error_holder:
                raise RuntimeError("Transformers backend failed during generation") from error_holder[0]
            final_event = result_holder.get("event")
            if final_event is None:
                raise RuntimeError("Transformers backend did not return generation metadata")
            yield final_event
        finally:
            cancel_event.set()
            if worker.is_alive():
                await asyncio.to_thread(worker.join, 2.0)

    async def close(self) -> None:
        return None


class VLLMBackend:
    """GPU-oriented continuous-batching backend using vLLM's async scheduler."""

    name = "vllm"
    device = "cuda"
    supports_batching = True

    def __init__(self, engine: Any, tokenizer: Any, sampling_params_cls: Any):
        self.engine = engine
        self.tokenizer = tokenizer
        self.sampling_params_cls = sampling_params_cls

    @classmethod
    def from_pretrained(
        cls,
        model_path: str,
        *,
        max_model_len: int,
        max_num_seqs: int,
        max_num_batched_tokens: int,
        tensor_parallel_size: int,
        gpu_memory_utilization: float,
    ) -> "VLLMBackend":
        try:
            from vllm import SamplingParams
            from vllm.engine.arg_utils import AsyncEngineArgs
            from vllm.engine.async_llm_engine import AsyncLLMEngine
        except ImportError as exc:
            raise ModelArtifactError(
                "RUSA_INFERENCE_BACKEND=vllm requires the optional vLLM runtime"
            ) from exc

        path = str(Path(model_path).resolve(strict=True))
        engine_args = AsyncEngineArgs(
            model=path,
            tokenizer=path,
            trust_remote_code=False,
            tensor_parallel_size=tensor_parallel_size,
            max_model_len=max_model_len,
            max_num_seqs=max_num_seqs,
            max_num_batched_tokens=max_num_batched_tokens,
            gpu_memory_utilization=gpu_memory_utilization,
            dtype="auto",
            disable_log_stats=True,
        )
        engine = AsyncLLMEngine.from_engine_args(engine_args)
        tokenizer = engine.get_tokenizer()
        if not getattr(tokenizer, "chat_template", None):
            raise ModelArtifactError("The vLLM tokenizer must provide a chat_template")
        return cls(engine, tokenizer, SamplingParams)

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> GenerationResult:
        chunks: list[str] = []
        result: GenerationResult | None = None
        async for event in self.stream(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            if event.delta:
                chunks.append(event.delta)
            if event.finish_reason:
                result = GenerationResult(
                    content="".join(chunks).strip(),
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    finish_reason=event.finish_reason,
                )
        if result is None or not result.content:
            raise RuntimeError("vLLM did not produce a completion")
        return result

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[GenerationEvent]:
        prompt_ids = self.tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
        )
        sampling = self.sampling_params_cls(
            max_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        request_id = uuid.uuid4().hex
        previous_text = ""
        finished = False
        try:
            async for request_output in self.engine.generate(
                {"prompt_token_ids": prompt_ids},
                sampling,
                request_id,
            ):
                if not request_output.outputs:
                    continue
                output = request_output.outputs[0]
                current_text = output.text
                delta = current_text[len(previous_text):]
                previous_text = current_text
                if delta:
                    yield GenerationEvent(delta=delta)
                if request_output.finished:
                    reason: Literal["stop", "length"] = (
                        "stop" if output.finish_reason == "stop" else "length"
                    )
                    yield GenerationEvent(
                        finish_reason=reason,
                        prompt_tokens=len(prompt_ids),
                        completion_tokens=len(output.token_ids),
                    )
                    finished = True
        finally:
            if not finished:
                try:
                    await self.engine.abort(request_id)
                except Exception:
                    logger.warning("Could not abort cancelled vLLM request")

    async def close(self) -> None:
        shutdown = getattr(self.engine, "shutdown_background_loop", None)
        if shutdown:
            result = shutdown()
            if asyncio.iscoroutine(result):
                await result


def load_inference_backend(
    model_path: str,
    *,
    backend_name: str,
    timeout_seconds: float,
    max_model_len: int,
    max_num_seqs: int,
    max_num_batched_tokens: int,
    tensor_parallel_size: int,
    gpu_memory_utilization: float,
) -> InferenceBackend:
    selected = backend_name.strip().lower()
    if selected not in {"auto", "transformers", "vllm"}:
        raise ModelArtifactError("RUSA_INFERENCE_BACKEND must be auto, transformers, or vllm")

    if selected == "auto":
        try:
            import torch
            import vllm  # noqa: F401

            selected = "vllm" if torch.cuda.is_available() else "transformers"
        except ImportError:
            selected = "transformers"

    if selected == "vllm":
        return VLLMBackend.from_pretrained(
            model_path,
            max_model_len=max_model_len,
            max_num_seqs=max_num_seqs,
            max_num_batched_tokens=max_num_batched_tokens,
            tensor_parallel_size=tensor_parallel_size,
            gpu_memory_utilization=gpu_memory_utilization,
        )
    return TransformersBackend.from_pretrained(
        model_path,
        timeout_seconds=timeout_seconds,
        max_model_len=max_model_len,
    )
