# RusaAi Architecture Decision Records

Use the full ADR title or filename in cross-repository references. Do not refer to an ADR only by its numeric identifier until the numbering collision below is resolved.

## Architecture decisions

| Decision record | Status | Scope |
|---|---|---|
| [ADR-0001 — RusaAi-Edge v2.0 CPU-first baseline](ADR-0001-rusaai-edge-v2-cpu-first-baseline.md) | Accepted for repository alignment; full architecture remains proposed | CPU-first model target, admission limits, deployment ownership |
| [ADR-0002 — Canonical Shared Agent/Worker Knowledge Store](ADR-0002-canonical-shared-agent-worker-knowledge-store.md) | Accepted by project owner for implementation planning; production activation not authorized | Selects RusaAi `rusa_knowledge` as canonical shared Agent/Worker store, subject to gates |
| [ADR-0002 — RusaModels owns the Model Plane](ADR-0002-rusamodels-model-plane-boundary.md) | Accepted for repository integration alignment | RusaAi Control Plane / RusaModels production Model Plane boundary |

## Numbering governance

**OPEN_DECISION:** two files under `docs/adr/` currently use the identifier `ADR-0002`. Their scopes are distinct, but the duplicate identifier is ambiguous in review comments, links, and automated references.

Until a separately reviewed numbering correction is approved:

- Use the full title or exact filename.
- Preserve existing file paths and links.
- Do not silently renumber or alter the accepted decision status.
- Resolve the identifier collision through a dedicated documentation-only change with a repository-wide reference check.

## Cross-repository architecture\n\n- [RUSA AI Ecosystem Architecture Reconciliation v1.0](../architecture/ECOSYSTEM_ARCHITECTURE_RECONCILIATION_v1.0.md) — service ownership, system context, data flows, trust boundaries and cross-repository contract inventory.\n\n## Related decision records

- [ADR-TRAIN-001 — Foundation checkpoint](../../documentation/adr/ADR-TRAIN-001-foundation-checkpoint.md) — training/model foundation scope.
