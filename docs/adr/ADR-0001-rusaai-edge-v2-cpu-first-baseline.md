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
   target-device memory validation. RusaModels owns the GGUF artifact and
   runtime; RusaAi calls the authenticated RusaModels Model Server through its
   Model Access Gateway.
4. Replace semaphore waiting accounting with a bounded FIFO admission queue
   that handles timeout and cancellation without leaking a reserved slot.
5. Set rusa-cpu-16 (16-GB) to three waiting requests. Configure rusa-edge-8
   (8-GB) to one waiting request and add a dedicated RusaAi Control Plane
   Compose resource overlay. Its four service memory ceilings total 2 GiB;
   RusaModels Model Plane memory is budgeted separately. No combined 8-GB
   Model Plane/Control Plane fit is certified by these limits.
6. Keep the current six-role AICode SFT experiment isolated from the
   RusaAi-Edge neural-core target.
7. Use `rusaai-cpu-smoke-test` as the CPU sample alias while no approved
   checkpoint/GGUF exists. Reserve `rusaai-edge-1.54b` for an audited
   checkpoint matching the exact reference dimensions; use
   `rusaai-edge-derived` if the selected checkpoint differs.

## Consequences

- The previous MoE/Opus blueprint is removed from the active model target.
- The CPU sample profile uses a smoke-test alias and does not misidentify an
  arbitrary GGUF as the not-yet-built RusaAi-Edge model.
- The API admission queue is bounded and FIFO. With the PostgreSQL scheduler
  configured, API replicas share durable queue state and active slots; the
  in-memory controller is a development fallback.
- Worker inference passes through the RusaAi API gateway and the same admission
  controller. The Worker never calls llama.cpp or the RusaModels runtime directly.
- RusaModels owns model registry, artifact lifecycle, runtime loading and
  residency. The full model checkpoint selection, tokenizer selection,
  training, and target-device benchmarks remain future work.
- Existing AICode, tenant isolation, queue, audit, sandbox, and CI controls
  remain in place.

## Validation

- Unit tests assert the exact model dimensions and parameter count.
- Unit tests cover queue capacity, FIFO handoff, timeout cleanup, cancellation,
  durable PostgreSQL slot leasing, and invalid release.
- Compose tests assert the RusaAi-to-RusaModels Model Plane boundary, one active
  inference, the smoke-test model alias, tenant-scoped Worker gateway routing,
  and bounded 8-GB Control Plane memory/queue settings.
- Full repository CI is the final gate for this branch. No production
  certification is claimed by this ADR.
