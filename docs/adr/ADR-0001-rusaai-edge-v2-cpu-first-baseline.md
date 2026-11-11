# ADR-0001: RusaAi-Edge v2.0 CPU-first baseline

- Date: 2026-11-11
- Status: Accepted for this repository alignment change; full architecture remains proposed
- Source: RusaAi_Architecture_v2.0.md, version 2.0.0

## Context

The repository's model blueprint described a 96-layer, 12,288-hidden-size
MoE design and an Opus-named placeholder. Those values do not match the
supplied v2.0 target. The API admission controller also used semaphore-based
waiting accounting that could over-admit concurrent arrivals before the first
request acquired the semaphore. CPU defaults allowed four waiting requests,
while the 16-GB v2.0 profile specifies at most three.

The repository already has a private llama.cpp GGUF CPU deployment path,
tenant-aware AICode workers, policy enforcement, and an optional legacy
Qwen3-4B worker-contract QLoRA experiment. Those existing systems must not be
misrepresented as a selected or trained RusaAi-Edge model.

## Decision

1. Set the design target to RusaAi-Edge-1.54B, model ID
   rusaai-edge-1.54b, with the exact dense Pre-Norm Transformer dimensions
   and 1,541,277,696 reference parameters in the source architecture.
2. Keep the core foundation checkpoint family non-Qwen and explicitly mark
   checkpoint, tokenizer, artifact, training, and benchmark states as pending.
3. Use llama.cpp with GGUF Q4_K_M for the CPU deployment baseline, GPU
   offload zero, one active generation, 4K initial context, and 8K only after
   target-device memory validation.
4. Replace semaphore waiting accounting with a bounded FIFO admission queue
   that handles timeout and cancellation without leaking a reserved slot.
5. Set rusa-cpu-16 (16-GB) to three waiting requests. Configure rusa-edge-8
   (8-GB) to one waiting request in .env.cpu-8gb.example as a conservative
   implementation choice; the source itself specifies only a small bounded queue.
6. Keep the current six-role AICode SFT experiment isolated from the
   RusaAi-Edge neural-core target.

## Consequences

- The previous MoE/Opus blueprint is removed from the active model target.
- The CPU Compose profile and model defaults use the canonical model ID.
- The API admission queue is FIFO and bounded within the API process.
- llama.cpp's one parallel slot remains the cross-process guard because
  worker inference currently bypasses the API admission queue.
- The full shared cross-process scheduler, model registry, residency manager,
  model checkpoint selection, tokenizer selection, training, and benchmarks
  remain future work.
- Existing AICode, tenant isolation, queue, audit, sandbox, and CI controls
  remain in place.

## Validation

- Unit tests assert the exact model dimensions and parameter count.
- Unit tests cover queue capacity, FIFO handoff, timeout cleanup, cancellation,
  and invalid release.
- Compose tests assert one active inference, the canonical model ID, CPU-only
  offload, and the configured batch/ubatch defaults.
- Full repository CI is the final gate for this branch. No production
  certification is claimed by this ADR.
