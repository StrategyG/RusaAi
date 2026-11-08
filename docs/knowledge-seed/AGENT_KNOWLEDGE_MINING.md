# RUSA AI Agent Knowledge Mining Catalog

**Version:** 1.0.0  
**Status:** Mining baseline / proposed, not a published production corpus  
**Architecture:** Centralized RAG with agent-scoped retrieval  
**Scope:** 10 canonical agents, 8 Agentic DevOps agents, 2 cross-cutting DevOps roles, and 6 AICode runtime worker profiles

## 1. Purpose and evidence boundary

This catalog records knowledge mined from the four project-supplied Markdown documents and accessible StrategyG GitHub repositories. It defines what each agent should retrieve, which sources are authoritative, and what must be verified before publication.

It does not claim that external MCP providers are connected, that Dropbox content has been read, or that new records have been embedded into a live database.

Source documents are preserved as supplied. Similar agent names are not merged. A source's design status is not upgraded merely because similar code exists in another repository.

## 2. Source authority and trust tiers

| Tier | Source class | RUSA use |
|---|---|---|
| A — Project authority | Owner-supplied RUSA architecture, agent specifications, ADR and lifecycle documents | Defines RUSA intent and roles. Preserve each source's status and version. |
| B — RUSA implementation evidence | StrategyG/RusaAi code, migrations, tests, runbooks and CI evidence | Describes code present at a specific commit. Does not by itself prove production deployment. |
| C — Supporting engineering patterns | RAG_Cerdas, im19-universal-mcp, Cloudbrowser, Universal-Generative-AI, android-ai-universal-connector, ai-notebook-studio-v | Reusable patterns and candidate designs. Not automatically RUSA policy or implemented RUSA capability. |
| D — Live external MCP/provider evidence | Authenticated MCP resources, tools and provider responses from configured endpoints | Not collected in this mining pass. Must carry principal, provider, timestamp, scope, source and untrusted-data labels. |
| E — General external references | Official protocol specifications and vendor documentation | Future research input. Pin exact version/date and distinguish from project decisions. |

### Initial source registry

| Source ID | Source | Role in mining | Status caveat |
|---|---|---|---|
| RUSA-ARCH-001 | Uploaded RUSA_AI_Architecture.md | Four-pillar architecture overview | Only 390 bytes; detail is limited. |
| RUSA-AGENT-001 | Uploaded RUSA_AI_All_Agents-2.md | Ten canonical agent specifications | Source status unspecified; implementation unverified. |
| RUSA-COMM-001 | Uploaded ADR-RUSA-AI-Komunikasi-Agent(1).md | RPC, queue, event log, envelope, handoff and governance | Status is Diusulkan. |
| RUSA-DEVOPS-001 | Uploaded RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE(1).md | Eight lifecycle agents plus orchestrator and security guard | Proposed Architecture, version 1.0. |
| RUSA-IMPL-001 | StrategyG/RusaAi | Existing RAG, MCP, worker, policy and test implementation | Deployment is not inferred from code or CI. |
| PATTERN-RAG-001 | StrategyG/RAG_Cerdas | Knowledge mining, evidence binding, review and retrieval patterns | Supporting project; not automatically RUSA policy. |
| PATTERN-MCP-001 | StrategyG/im19-universal-mcp | MCP service registry, OAuth, scopes, read-only connectors and outbound router | README states no live Google/YouTube/ChatGPT connection is claimed. |
| PATTERN-BROWSER-001 | StrategyG/Cloudbrowser | Private RAG client, allowlisted MCP gateway, owner approval and project scope | Incremental opt-in, single-owner RAG; not a general federation platform. |
| PATTERN-UGAI-001 | StrategyG/Universal-Generative-AI | Intelligence/MCP/execution plane separation, model routing and durable workflow | Live IM19 connectivity is not verified. |
| PATTERN-CONNECTOR-001 | StrategyG/android-ai-universal-connector | Provider adapter, OAuth and action governance | Adapter registry is not proof that real provider adapters are connected. |
| PATTERN-PLATFORM-001 | StrategyG/ai-notebook-studio-v | Agent registry descriptors, effective grants, tenant authorization and event envelopes | Platform docs distinguish proposed baseline from implemented/verified capabilities. |

### High-value mined paths

**RUSA implementation**
- docs/RAG_MCP_OPERATIONS.md
- rusa_knowledge/service.py, store.py, embeddings.py, chunking.py, parsers.py, evaluation.py and safety.py
- rusa_mcp/server.py and client.py
- rusa_agents/contracts.py, agent_tools.py, tool_gateway.py and runtime.py
- migrations/007_rag_knowledge_pgvector.sql
- tests/test_knowledge_*.py and tests/test_mcp_*.py

**RAG mining patterns**
- RAG_Cerdas/docs/IM19_ARCHITECTURE.md
- RAG_Cerdas/im19/knowledge_mining/classifier.py
- RAG_Cerdas/im19/knowledge_mining/mining_engine.py
- RAG_Cerdas/im19/knowledge_review/review_engine.py
- RAG_Cerdas/im19/retrieval.py
- RAG_Cerdas/docs/IM19_SECURITY_REPORT.md

**MCP patterns**
- im19-universal-mcp/src/im19_mcp/registry.py, server.py, security.py, upstream_mcp.py, drive.py and youtube.py
- im19-universal-mcp/docs/ARCHITECTURE.md, GOVERNANCE.md, UPSTREAM_MCP_ROUTER.md and THREAT_MODEL.md
- Universal-Generative-AI/docs/IM19_MCP_INTEGRATION.md, src/ugai_mcp/runtime.py and policy.py
- Cloudbrowser/docs/KNOWLEDGE_MCP.md, cloudbrowser/mcp_gateway.py and rag_client.py
- android-ai-universal-connector/docs/CONNECTOR_ADAPTERS.md

**Agent platform patterns**
- ai-notebook-studio-v/docs/PLATFORM-ARCHITECTURE-V5.md
- ai-notebook-studio-v/docs/TENANT-AUTHORIZATION-V6.md
- ai-notebook-studio-v/apps/api/src/ai_notebook_api/domain/agent_registry.py
- ai-notebook-studio-v/apps/api/src/ai_notebook_api/security/policy.py
- ai-notebook-studio-v/apps/api/src/ai_notebook_api/domain/events.py

## 3. MCP mining findings — code catalog versus live connection

### RUSA Knowledge MCP

The RUSA MCP server defines:
- Tools: search_knowledge, get_document and list_collections.
- Resource: knowledge://capabilities.
- Prompt: answer_with_sources.
- Tenant identity is derived from an authenticated bearer token; caller-supplied tenant identity is not accepted.
- Retrieved content is untrusted evidence, not instructions.
- The MCP service is separately deployed and readiness-gated; installing its package does not activate it in the core API.

### IM19 Universal MCP

The inspected code defines:
- Resource: im19://capabilities.
- Prompt: im19_security_review.
- Tools for service discovery/capabilities/connection status, Google OAuth lifecycle, YouTube read operations, Drive metadata/read operations and a narrowly scoped approval-gated Drive folder-creation prototype.
- Registry marks YouTube, Drive and Cloudbrowser A2A as implemented capabilities; Gmail, Calendar, Docs, Sheets, Instagram, Facebook, TikTok, X, LinkedIn and GitHub are marked not implemented.
- README explicitly says no live Google, YouTube or ChatGPT connection is claimed. A stored OAuth credential is not live provider verification.
- drive_read_file requires separately consented restricted scope. drive_create_folder is disabled by default and approval-gated.
- The outbound MCP router is disabled unless an operator injects a connector registry and trusted secret/tenant resolvers.

### MCP trust rules adopted for RUSA

1. Discover tools, resources and prompts only from operator-configured endpoints.
2. Treat external tool descriptions, schemas, resources, prompts and results as untrusted input until reviewed.
3. Never let a model choose arbitrary MCP URLs, server IDs, tenant IDs, identities, credentials or scopes.
4. Keep server and tool allowlists operator-controlled and default-deny.
5. Read-only is the default. Mutations require action-bound human approval, exact target and payload hash, idempotency and audit.
6. Never index bearer tokens, OAuth codes, refresh tokens, API keys, cookies, private keys or raw secrets.
7. Preserve provider, account/tenant, principal, scope, timestamp and source provenance for collected data.
8. A catalog entry or successful discovery call does not prove a live provider connection or permission to read its content.
9. The inspected UGAI policy has a hard-deny set for service authorization/confirmation/disconnection, Drive file read/create, Drive write-approval status, and Cloudbrowser task read/list tools. Do not bypass these restrictions by copying the IM19 tool schema into RUSA.
10. Treat MCP tool schema, tool annotation and resource metadata as capability evidence only; do not convert a discovered tool into an agent permission automatically.

### MCP-to-agent knowledge map

| MCP/source surface | Evidence available in this mining pass | Candidate consumers | Activation boundary |
|---|---|---|---|
| RUSA Knowledge MCP | search_knowledge, get_document, list_collections, knowledge://capabilities, answer_with_sources | All workers for shared knowledge; Memory & Knowledge for source/provenance workflows | Read-only; authenticated tenant; shared-only unless an agent principal is supplied by a trusted runtime. |
| IM19 capability registry | list_available_services, get_service_capabilities, get_connection_status, im19://capabilities | Supreme Orchestrator, System Architect, Planner, Security & Policy, DevOps Orchestrator | Discovery/status only by default. Does not authorize reading provider data or performing actions. |
| IM19 YouTube adapter | channel/video/analytics read tools | Evaluation & Reporting, Memory & Knowledge, Planner when external learning material is explicitly requested | Registry says implemented; no live account connection was verified. |
| IM19 Drive adapter | search/list/metadata/read file tools | System Architect, Planner, Memory & Knowledge, Software Engineer, QA | Requires an authorized account and scopes; restricted file-read scope is not assumed. |
| IM19 Drive folder creation | approval-gated prototype | No default agent consumer | Disabled by default; exact action approval required. |
| Cloudbrowser MCP/A2A gateway | allowlisted browser/agent capabilities and private RAG integration | Supreme Orchestrator, System Architect, QA, DevOps Orchestrator | Operator-configured allowlist and owner approval; no general remote execution. |
| UGAI MCP adapter/policy | MCP runtime, model routing, policy boundaries and workflow integration patterns | Supreme Orchestrator, System Architect, Security & Policy, Evaluation & Reporting | Supporting design/code pattern; live IM19 connectivity not verified. |
| Android provider adapter | provider registry, OAuth/action authorization patterns | Security & Policy, System Architect, DevOps | Adapter contract pattern only; no provider is assumed connected. |
| GitHub repository integration | source files, tests, migrations, commits and PR metadata accessible in the current repository connector | Analyst/Architect/Developer/Debugger/Tester/Security runtime workers; canonical Software Engineer, QA, System Architect | Repository connector access is distinct from an IM19 GitHub MCP provider; IM19 registry marks GitHub not implemented. |
| Dropbox folder | User-provided shared folder URL | Memory & Knowledge and all agents only after source approval | Not mined in this pass because Dropbox access was declined/not connected. |

MCP data is a source of facts about tool capabilities and external systems, not an instruction channel. Every MCP result must be stored as evidence with endpoint identity, tool/resource name, principal, scopes, timestamp, response hash and review status.

### Live-source limitation

This pass mined repository definitions and documentation, not live remote MCP resources. Dropbox was not connected, so the shared Dropbox folder and its files were not read. MCP capability catalogs above must not be represented as live data acquisition.

## 4. Knowledge extraction model

Use the 14 knowledge types identified in the RAG_Cerdas mining implementation as a candidate extraction vocabulary:

- concept
- definition
- principle
- methodology
- causal_relation
- formula
- procedure
- case_study
- risk
- limitation
- factual_claim
- opinion
- assumption
- supporting_evidence

For RUSA architecture and agent sources, additionally tag records with domain labels such as architecture, agent_contract, communication, workflow, security, operations, testing, evaluation, mcp_integration and knowledge_governance.

The RAG_Cerdas classifier has a separate hierarchical subject taxonomy with 16 top-level categories (including Technology, Artificial Intelligence, Business, Finance, Law, Education and Health) and subcategories. This is a reusable classifier design pattern, not the RUSA domain taxonomy; RUSA must use the domain labels above and add project-specific subdomains only through review.

RAG_Cerdas also provides five logical review roles: Content Integrity, Knowledge Quality, Fact Verification, Contradiction Analysis and Institutional Knowledge Auditor. Its quality model weights extraction integrity, provenance, evidence strength, consistency, source credibility and freshness. Missing dimensions remain null and produce NEEDS_REVIEW rather than being guessed. The score is explicitly uncalibrated and cannot certify external truth or human approval.

Its extractive triage uses explicit review rubrics for quote traceability, context, claim type, comparison/contradiction classification and quarantine recommendation. A direct-contradiction label is not treated as certified fact; the source excerpts and human review remain required.

Extraction controls:
- Bind every mined claim to an exact source excerpt and source locator.
- Keep model-extracted claims unverified until review.
- Exact quote presence is not proof that a paraphrased claim is entailed.
- Keep confidence null unless a calibration procedure exists.
- Separate fact, opinion, assumption, proposal, implementation evidence and operational observation.
- Detect duplicates and candidate relationships; do not auto-label contradictions without checking definition, context, period and methodology.
- Require human review for ambiguous classifications, consequential claims and source conflicts.

## 5. Per-agent knowledge profiles

The ten canonical profiles come from RUSA_AI_All_Agents-2.md. The eight lifecycle profiles and two cross-cutting roles come from RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE(1).md. Six additional AICode runtime worker profiles come from StrategyG/RusaAi/docs/aicode-agent-enterprise.md and the WorkerRole enum. The profile IDs for the ten canonical agents are normalized slugs from their source headings; they are not a new authority to merge identities.

| Agent profile | Primary knowledge domains | Retrieval focus / questions |
|---|---|---|
| supreme-orchestrator | agent_capabilities, workflow, dependency, task_state, policy, evidence | Which agent is responsible? What dependencies/gates remain? What evidence is required before completion? |
| system-architect | system_architecture, component_boundary, API, database, event, ADR, scalability, reliability | What is the documented design? Which decision is proposed versus approved? What dependencies/contracts change? |
| planner | requirements, scope, backlog, priority, milestone, effort, risk, acceptance_criteria | What are current and target conditions? Which work packages, dependencies, risks and criteria are evidenced? |
| task-decomposer | task_contract, DAG, dependency, parallelism, handoff, task_input_output | Can tasks run in parallel? What blocks each task? Does every task have an owner, output and acceptance test? |
| software-engineer | repository_conventions, code, API_contract, schema, test, Git, compatibility | Which files/contracts are authoritative? What tests and compatibility constraints apply? |
| devops | compute, storage, network, containers, CI_CD, deployment, database_ops, backup, monitoring, recovery | What deployment is approved? What health/rollback/backup evidence is required? |
| qa-testing | unit, integration, API, E2E, regression, performance, agent_behavior, model_evaluation | Which acceptance criteria and test evidence prove correctness? What failures remain? |
| security-policy | identity, authentication, authorization, RLS, OAuth, secrets, threat_model, prompt_injection, audit | Which principal/scope is trusted? What data/action is allowed? What is implemented versus unverified? |
| memory-knowledge | source_registry, parsing, chunking, extraction, embedding, retrieval, provenance, versioning, retention | What is the exact source/locator? Which records are stale, duplicate, conflicted or unverified? |
| evaluation-reporting | retrieval_metrics, agent_metrics, workflow_outcome, quality, cost, latency, reliability | Which benchmark supports the result? Are metrics measured, labeled, comparable and reproducible? |
| rusa_plan_agent | requirements, issue, backlog, acceptance_criteria, dependency, risk, operations_feedback | What plan, requirements, backlog and risk assessment are supported by project evidence? |
| rusa_code_agent | source_code, repository_layout, coding_standard, API_contract, Git, pull_request | Which code change implements the approved task? Which checks and artifacts support it? |
| rusa_build_agent | build_config, dependency_lock, reproducibility, container, artifact_digest, SBOM | Can the artifact be reproduced and identified by immutable digest? What provenance is missing? |
| rusa_test_agent | test_strategy, test_case, fixture, test_result, security_test, acceptance | Which tests ran against which artifact digest? What failed, passed or was not run? |
| rusa_release_agent | release_policy, versioning, changelog, release_candidate, manifest, evidence_gate | Is the release candidate complete, approved and traceable to test/security evidence? |
| rusa_deploy_agent | deployment_policy, environment, infrastructure, approval, health_check, rollback | Is this target authorized? What deployment/health/rollback evidence is required? |
| rusa_operate_agent | runbook, incident, recovery, service_health, backup_restore, capacity | What runbook action is approved? What incident and recovery evidence exists? |
| rusa_monitor_agent | logs, metrics, traces, SLO, alert, anomaly, capacity, feedback | What is the measured signal, time window, baseline and threshold? |
| rusa_devops_orchestrator | lifecycle_state, stage_dependency, handoff, gate, event, retry, cancellation | Which stage may proceed? What evidence/gate failed? What must be stopped or escalated? |
| rusa_security_guard_agent | secret_scan, dependency_security, container_security, tenant_isolation, authorization, privacy, audit | Which cross-stage policy applies? What evidence blocks or clears the workflow? |

### Identity overlap — do not merge automatically

| Potential overlap | Treatment |
|---|---|
| planner ↔ rusa_plan_agent | Related responsibilities; keep two source identities until System Architect approves a canonical mapping. |
| software-engineer ↔ rusa_code_agent | Related responsibilities; keep separate. |
| qa-testing ↔ rusa_test_agent | Related responsibilities; keep separate. |
| devops ↔ eight lifecycle agents | DevOps is a broad canonical role; lifecycle agents are stage-specific. Do not flatten. |
| supreme-orchestrator ↔ rusa_devops_orchestrator | Different orchestration scopes; keep separate. |
| security-policy ↔ rusa_security_guard_agent | Related security roles; keep separate until governance defines scope and delegation. |

### Six AICode runtime worker scopes

These are implemented worker roles in the RUSA-AI AICode subsystem, not aliases for the ten higher-level documented agents. The feature branch binds each authenticated WorkerRole to a server-derived scope ID:

| Runtime role | Trusted scope ID | Documented responsibility | Existing tool permission |
|---|---|---|---|
| Analyst | aicode-analyst | Repository inventory, relevant code reading, defect localization | List, read, diff, knowledge read |
| Architect | aicode-architect | Design and implementation plan | List, read, diff, knowledge read |
| Developer | aicode-developer | Approved code, test and documentation changes | List, read, write, diff, knowledge read |
| Debugger | aicode-debugger | Diagnose failures and propose fixes | List, read, registered checks, diff, knowledge read |
| Tester | aicode-tester | Execute pytest validation | List, read, pytest check, diff, knowledge read |
| Security | aicode-security | Ruff, Bandit and compile checks | List, read, registered checks, diff, knowledge read |

The scope ID is derived from the trusted WorkerRole in the host gateway, not from model-generated arguments. These six scopes remain separate from the ten canonical and eight lifecycle profiles until System Architect approves a formal identity mapping.

### Curated per-agent source-section packs

The feature branch now includes `docs/knowledge-seed/agent_knowledge_map.json` and `rusa_knowledge/agent_mining.py`. The map defines exact Markdown heading subtrees for the ten canonical agents and ten Agentic DevOps/cross-cutting roles. The miner:

- verifies the four original source files against the pinned source manifest before extracting anything;
- requires each configured heading to exist exactly once and ignores headings inside fenced code blocks;
- extracts heading subtrees without rewriting their text;
- records original source line locators, source SHA-256, section SHA-256 and map SHA-256;
- creates one stable derived document URI per agent with `knowledge_scope=agent_specific` and exactly one `agent_id`;
- runs a non-writing preflight by default; database writes and embedding calls require explicit `--apply`.

The four original source documents remain shared platform context. The 20 derived packs provide focused per-agent retrieval rather than claiming that the original source documents are confidential or inaccessible to other agents. Six AICode runtime workers still require a separately reviewed source map based on their implementation/code knowledge; they are not silently mapped to the 20 documented roles.

## 6. Centralized RAG retrieval model

### Target model

Keep one physical tenant-scoped knowledge store and one shared RUSA knowledge collection. Store shared and agent-targeted records in that corpus with metadata such as:

- knowledge_scope: shared or agent_specific
- agent_ids: normalized agent profile IDs
- knowledge_domain
- source_id, source_uri, source_version, source_commit
- source_authority_tier
- source_status_class
- verification_status
- implementation_status
- section_path, content_hash, ingestion_run_id
- access_policy, tenant_id, project_id

The authenticated server/runtime must inject agent_id. The model must not choose or override it. Every record with agent_ids must explicitly declare knowledge_scope; missing scope is rejected rather than inferred as shared. Retrieval returns records where the scope is shared or the authenticated agent is listed in agent_ids, after tenant/project authorization.

### Current implementation gap in RUSA-AI

The base implementation inspected in StrategyG/RusaAi uses PostgreSQL 16 + pgvector with 1536-dimensional embeddings, vector plus PostgreSQL full-text retrieval, and weighted RRF (0.7 vector / 0.3 lexical). It stores document metadata but its base search path filters by tenant and collection only; it does not apply agent_id or project metadata filters. The base worker gateway also accepted a model-selected collection value.

The current feature branch adds agent- and project-aware retrieval:
- trusted WorkerRole is converted by the host gateway to a server-owned scope ID such as aicode-analyst;
- the authorized repository target is normalized by the host to a stable project scope such as strategyg--rusaai;
- vector and lexical candidates are filtered to shared records plus records whose agent_ids contain the authenticated worker scope and whose optional project_id matches;
- document reads and collection counts apply the same visibility filter;
- worker tools do not accept model-selected collection, agent_id or project_id arguments;
- collection counts and reads apply the same visibility boundary;
- general RUSA Knowledge MCP requests without an authenticated agent identity see shared records only;
- metadata validation distinguishes shared and agent_specific records and validates project identifiers;
- migration 010 snapshots prior document metadata, chunk text and embeddings when the content/title/metadata fingerprint changes; identical re-ingestion does not create a duplicate version;
- historical version reads intersect the current document policy with the historical version policy, preventing project or agent scope changes from widening access to old snapshots.

These are code changes in the open feature branch, not a merged or production deployment. Authenticated per-agent MCP principals, live database ingestion and end-to-end production verification remain outstanding. The inspected RUSA path still has no reranker and uses character-based 1200-character chunks with 180-character overlap.

Therefore the branch establishes an initial per-runtime-worker and per-project retrieval boundary, while the full 20 documented architecture/lifecycle profiles remain catalogued for later explicit runtime binding.

### Remaining engineering work and status

1. **Worker agent identity — implemented in this feature branch.** The host derives AICode scope IDs from WorkerRole. Authenticated agent principals for standalone MCP clients are still not implemented; those requests remain shared-only.
2. **Profile registry — catalogued, not yet a runtime registry.** Ten canonical, eight lifecycle, two cross-cutting and six AICode worker profiles are recorded in agent_profiles.yaml. The six AICode scopes are intentionally not auto-merged with the documented architecture roles.
3. **Agent metadata filters — implemented in this feature branch.** Both vector and lexical candidates use the shared-or-agent_ids filter before rank fusion; document reads and collection counts apply the same filter.
4. **Tenant + agent + project isolation — implemented in this feature branch for AICode workers.** Tenant RLS is retained; the host derives project scope from the already-authorized repository, and project-bound agent-specific records are visible only to that agent in that project. PostgreSQL integration tests cover cross-agent and cross-project denial. This is an application-level project metadata filter, not a separate project-level RLS policy.
5. **Collection selection — worker model argument removed in this branch.** The worker uses the configured collection. Standalone MCP retains its existing collection argument but has no agent identity and can only retrieve shared records.
6. **Immutable source version history — implemented in this feature branch.** Migration 010 stores immutable prior metadata and chunk/embedding snapshots. History reads require both the current and historical visibility policies; production deployment and retention operations remain outstanding.
7. **Per-agent evaluation sets — partially prepared.** The evaluator accepts stable `relevant_source_uris` labels before database UUIDs exist. The 20-profile source map supplies stable agent document URIs, but relevance judgments still require human review after a successful preflight.
8. **Vector store — retain pgvector.** No measured evidence justifies a second vector database.
9. **MCP source sync — outstanding.** The current catalog is mined from repository code/docs, not live remote MCP resources. Add live connectors only after endpoint, principal, scopes, allowlist and approval are configured.

## 7. Retrieval quality and publication gates

Candidate metrics:
- Recall@5, MRR@10 and NDCG@10 (the current RUSA evaluator implements Recall@K, MRR@K and NDCG@K).
- Citation/source coverage and exact locator validity.
- Evidence support rate and unsupported-claim rate.
- Abstention accuracy for questions with no eligible evidence.
- Conflict detection precision/recall.
- Agent-scope leakage rate (target: zero).
- Tenant/project isolation leakage rate (target: zero).
- Per-agent source URI coverage and section-locator validity.
- Prompt-injection resistance for retrieved external content.
- Retrieval latency and index freshness.

Build a manually reviewed gold set for every agent. Each case must include query, expected source IDs/sections, allowed shared sources, forbidden cross-agent sources, and an explicit unanswerable class where applicable. Do not manufacture relevant document UUIDs before ingestion; map source IDs to actual document IDs after seed ingestion.

Publish only after source checksum/parser validation, exact evidence locator retention, classification review or pending label, source/implementation status retention, access-policy evaluation, pinned embedding model/version/dimension, retrieval/citation evaluation and quarantine of failed/conflicted records.

## 8. Initial implementation sequence

### Phase A — Source mining
- Register four owner-supplied source documents.
- Mine RUSA code, tests and migration as implementation evidence.
- Mine supporting repository patterns with lower authority.
- Define all 20 agent profiles and identity overlap.
- Record MCP catalog versus live-connectivity boundary.

### Phase B — Profile-aware metadata
- Add agent profile validation.
- Add source/domain/agent metadata to ingestion.
- Preserve shared versus agent-specific visibility.
- Implement tenant-scoped append-only document-version snapshots and scoped history reads in migration 010; deployment and retention operations remain pending.

### Phase C — Trusted retrieval scope
- Resolve agent identity server-side.
- Map runtime roles explicitly.
- Filter shared + agent-specific evidence in SQL before RRF.
- Restrict collection selection.
- Add cross-agent and cross-tenant isolation tests.

### Phase D — Per-agent knowledge corpus
- Ingest source material with exact evidence and provenance.
- Extract claims, procedures, risks and contracts.
- Human-review conflicts and consequential claims.
- Publish reviewed records and embeddings.

### Phase E — Evaluation and continuous mining
- Build per-agent gold sets.
- Evaluate retrieval, citation, abstention and scope isolation.
- Add approved MCP discovery and source sync.
- Re-index on source revision changes while retaining prior snapshots (implemented in the feature branch; production retention/restore operations remain pending).
- Keep operational events and episodic memory separate from architecture facts.

## 9. Non-goals for this increment

- No Dropbox folder ingestion; Dropbox access is not connected in this session.
- No live Google Drive, YouTube, GitHub MCP, Gmail or Calendar provider claim.
- No external MCP write operations.
- No automatic merging of overlapping agent definitions.
- No replacement of pgvector with Qdrant.
- No claim that the four seed documents have been embedded or published.
