# RUSA AI Four Foundations — Open Architecture Decisions

**Snapshot:** 2026-11-09  
**Status:** decision register; recommendations below are not approved decisions.

| ID | Decision requiring owner approval | Source fact / current condition | Recommended treatment | Accountable owner |
|---|---|---|---|---|
| D-001 | Canonical global orchestrator name | All Agents says Supreme Orchestrator; ADR says Executive Orchestrator | Select one canonical ID; do not run both as peer coordinators. `rusa_supreme_orchestrator` is a candidate, not an approved decision | Product + Architecture |
| D-002 | Repository placement | DevOps Lifecycle defines Repository as shared evidence/artifact layer; four-pillar Architecture does not list it as a pillar | Treat as shared capability until product placement is approved | Product + Platform |
| D-003 | Social placement | DevOps Lifecycle defines optional owner-authorized publishing | Keep disabled by default; decide whether it is an Ecosystem module | Product |
| D-004 | Platform Foundation scope | Source lists identity, authorization, policy, event, audit, billing, observability, Developer API | Treat as cross-cutting infrastructure, not a fifth pillar | Platform |
| D-005 | Developer API ownership | Ecosystem includes Developer API; Platform Foundation also lists Developer API | Ecosystem owns developer-facing product/API catalogue; Platform owns gateway, identity, quotas, and rate-limit infrastructure | Ecosystem + Platform |
| D-006 | ADR-COMM-001 adoption | ADR status is Diusulkan | Keep proposed until approved; publish a versioned compatibility/migration ADR before production use | Architecture |
| D-007 | Task/event contract layering | All Agents, DevOps YAML, and ADR define different levels of contract | Preserve transport envelope + immutable task specification + immutable result/evidence references | Platform |
| D-008 | Canonical state machine | Source vocabularies differ: PENDING/READY/RUNNING/COMPLETED vs queued/running/passed/etc.; ADR events are not states | Define one canonical state machine and explicit adapters; do not use event names as state names | Platform + DevOps |
| D-009 | Approval event contract | Source has approval_required; ADR v1 event enum lacks generic approval request/decision events | Add explicit approval request/granted/denied contract and audit reference | Governance |
| D-010 | QA verdict event and digest | ADR says task completion is distinct from final run completion after QA, but v1 event list lacks a QA verdict event | Add QA verdict bound to exact source commit and artifact digest | Quality + Platform |
| D-011 | Broker topology | ADR proposes RabbitMQ for commands and Kafka for event log, while noting two-system operational burden | Validate existing infrastructure and operational ownership first; do not deploy two brokers by default | SRE + Platform |
| D-012 | DevOps authority implementation | PR #68 and #70 are parallel Phase 4A approaches; #69 depends on #68 | Select one canonical authorization path; selectively port unique controls from the other | Security + DevOps |
| D-013 | Business initial domain | Architecture gives broad scope only; no process/KPI/system-of-record/approval matrix is supplied | Select one pilot process and owner before coding workflow automation | Business owner |
| D-014 | Ecosystem initial scope | Architecture gives marketplace, Developer API, integrations, and agent ecosystem at a high level | Start with private beta; define publisher trust, package signing, sandbox, certification, quotas, revocation, and support | Ecosystem owner |
| D-015 | Agent role mapping | Ten broad roles and eight lifecycle specialists overlap; current AICode runtime has six distinct worker roles | Maintain separate canonical IDs and explicit capability adapters; no duplicate dispatcher | Agent Platform |
| D-016 | Model provider and data policy | Main supports provider adapters; no trained RUSA weights are bundled | Approve model/provider, data residency, classification routes, retention, budget, and evaluation before activation | Intelligence + Security |
| D-017 | RAG evaluation corpus | RAG source query template and metadata override are empty; dense retrieval experiment not completed in reviewed local runtime | Create human-labeled held-out relevance set; report Recall@K/MRR/NDCG only after execution | Knowledge owner |
| D-018 | Predictive telemetry scope | Current experiment uses 1,000 sample hourly rows and five metrics; no one-billion-row run | Acquire representative labelled telemetry and run chronological backtests before incident-classifier claims | SRE + ML |
| D-019 | Production identity and Tool Gateway | Main has API-key/RBAC and sandbox boundaries; source ADR requires workload identity, mTLS, scoped grants, signed durable messages | Map source requirements to existing auth without replacing current controls; independently verify deployment controls | Security + Platform |
| D-020 | Product maturity language | Current repository and open PRs contain code, prototypes, and proposed contracts at different maturity levels | Label each capability as source-defined, main-verified, open-PR, experiment, or deployed; never collapse these statuses | Product + Engineering |

## Approval record

No decision in this register is considered accepted until the accountable owner records the chosen option, rationale, date, and affected ADR/schema version. Updating documentation alone does not approve a product or security decision.
