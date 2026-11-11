# Inference contract

The versioned JSON Schemas in this directory formalize the RusaAi v2.0
inference request and response envelope. They are additive contracts; the
existing OpenAI-compatible /v1/chat/completions endpoint has not yet been
migrated to consume these envelopes directly.

The contract pins the requested model identity to rusaai-edge-1.54b, makes
the CARBON/ASTRA capability profile and reasoning mode explicit, requires a
deadline, and rejects GPU offload in the CPU-first baseline. Keep these
schemas backward-compatible by adding a new versioned schema rather than
silently changing v1 fields.
