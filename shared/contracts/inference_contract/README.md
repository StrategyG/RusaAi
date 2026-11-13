# Inference contract

The versioned JSON Schemas in this directory formalize the RusaAi v2.0
inference request and response envelope. They are additive contracts; the
existing OpenAI-compatible /v1/chat/completions endpoint has not yet been
migrated to consume these envelopes directly.

The contract accepts the exact v2.0 reference identity `rusaai-edge-1.54b`
or the audited non-Qwen derived identity `rusaai-edge-derived` when the
selected checkpoint differs from the reference dimensions. The transport-only
`rusaai-cpu-smoke-test` alias is intentionally not an agent model identity.
The request makes CARBON/ASTRA capability, reasoning mode, deadline, and
CPU-only execution explicit. A completed response must include usage and
performance measurements; queued or non-completed responses may omit metrics
that have not yet been measured. Keep future changes versioned.
