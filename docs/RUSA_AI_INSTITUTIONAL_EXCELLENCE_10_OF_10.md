# RUSA AI Institutional Excellence Program — Target 10/10

**Status:** Target-state program and proposed control baseline; not an audit opinion or certification  
**Owner decision:** Hybrid model strategy; centralized RAG for all approved agent profiles and authorized project repositories; agent-aware MCP; governed autonomous engineering  
**Repository baseline:** `feat/rusa-knowledge-core-phase1`, draft PR #82  
**Assessment rule:** A control earns credit only when it is implemented, independently tested, evidenced, assigned an owner, and periodically reviewed. Documentation alone earns design credit, not operating-effectiveness credit.

## 1. Purpose and non-claim

This program raises RUSA AI from a promising engineering prototype toward an institutionally governed AI platform. A 10/10 score is a stretch target, not a guarantee. No certification, compliance, production readiness, or 10/10 rating is claimed by creating this document.

The program uses the following external frameworks as alignment references, not as automatic certification:
- ISO/IEC 42001 — AI management system.
- NIST AI Risk Management Framework — Govern, Map, Measure, Manage.
- ISO/IEC 27001 — information security management system.
- CMMI — engineering process capability and continuous improvement.
- OWASP guidance for LLM/application risks and secure software development, where applicable.

The exact applicable editions, legal obligations, control applicability, and audit scope must be confirmed by qualified security, legal, privacy, and compliance owners before making conformity claims.

## 2. Institutional scoring model

Weighted score = sum of each pillar's verified maturity score (0–10) multiplied by its weight. The assessor records evidence IDs, sample size, test date, commit/release, control owner, exceptions, and operating period.

| Pillar | Weight | 10/10 outcome evidence |
|---|---:|---|
| AI governance and accountability | 15% | Approved AI policy, accountable owners, AI system inventory, risk classification, impact assessments, change approvals, incident and exception governance |
| Enterprise architecture | 10% | Versioned architecture and ADRs, threat models, dependency maps, service SLOs, resilience design, reviewed architecture changes |
| RAG and data governance | 15% | Source ownership, lawful-use/consent basis, provenance, classification, retention/deletion, freshness, access controls, reviewed quality benchmarks, zero cross-scope leakage |
| MCP and security governance | 15% | Strong workload/agent identity, least privilege, short-lived grants, tool allowlists, sandboxing, idempotency, audit, threat testing, zero unauthorized side effects |
| Model lifecycle and training | 15% | Dataset/model registry, lineage, rights review, reproducible training, independent benchmarks, red-team, signed artifacts, staged rollout, rollback, drift monitoring |
| Validation and QA | 10% | Independent test strategy, risk-based acceptance criteria, held-out benchmarks, reproducible evidence, defect triage, release sign-off |
| Operational resilience | 10% | SLOs, monitoring, incident response, tested backup/restore and disaster recovery, capacity/cost controls, rollback and business continuity exercises |
| Compliance and auditability | 10% | Control mapping, evidence retention, access reviews, vendor assessments, privacy/security assessments, internal audit and independently closed findings |

### Maturity anchors

- 0: absent or unsafe.
- 1–2: ad hoc prototype; controls mostly undocumented or untested.
- 3–4: defined design and partial implementation; material gaps remain.
- 5–6: controls implemented and tested in non-production; limited operating evidence.
- 7–8: production controls operating with metrics, ownership, recurring review, and remediated findings.
- 9: independently assessed, resilient across realistic failure/adversarial scenarios, with sustained evidence.
- 10: sustained, independently verified effectiveness over a defined operating period, no unresolved critical/high control failures, continuous improvement demonstrated, and applicable external conformity independently assessed where claimed.

A weighted average must not hide a critical failure. Any confirmed cross-tenant disclosure, unauthorized privileged action, uncontained critical security incident, tampered model artifact, or material data-rights violation caps the overall rating at 4/10 until containment, remediation, retest, and independent closure.

## 3. Non-negotiable institutional invariants

1. Tenant, agent, project, repository, task, environment, and tool permissions originate only from trusted identity and policy context; never from model output.
2. Retrieved documents, repository files, tool outputs, web pages, and user-provided artifacts are untrusted data, not instructions.
3. Fail closed when identity, authorization, policy, provenance, source freshness, or required evidence is unavailable.
4. Models propose; deterministic policy and trusted services authorize. No model or agent can grant itself permission.
5. All side effects are scoped, auditable, bounded, idempotent where possible, and reconcilable after ambiguous failures.
6. Secrets and sensitive personal data are not embedded, logged, or used for training unless a documented lawful basis and approved control explicitly permit the operation.
7. No automatic training or knowledge promotion from unreviewed production traces or unverified agent outputs.
8. QA evidence is bound to the exact source commit and artifact digest being released.
9. Protected branch merges, production deployments, destructive migrations, secret/identity/policy changes, and material spend require the designated approval.
10. All high-risk operations have tested cancellation, recovery, and rollback paths.

## 4. Workstreams and required controls

### 4.1 AI governance and accountability

Deliver:
- AI system inventory: use case, owner, purpose, users, affected parties, model/provider, data classes, tools, environments, risk tier, deployment status.
- RACI for product owner, system owner, data owner, model owner, security, privacy, QA, operations, incident commander, and approver.
- AI acceptable-use policy, prohibited-use register, human oversight policy, model change policy, data policy, retention policy, incident policy, supplier policy.
- AI impact/risk assessments at design, material change, and periodic review.
- Formal exception register with compensating controls, expiry, and approver.
- Incident reporting, escalation, user communication, corrective action, and post-incident review.

Acceptance:
- 100% of deployed AI use cases have owner, risk tier, impact assessment, approved data/tool scope, and rollback plan.
- 100% of policy exceptions have named approver, rationale, compensating control, expiry, and review.
- No production model or agent is deployed outside the inventory.

### 4.2 Enterprise architecture and engineering governance

Deliver:
- Canonical service catalog, context/container/component diagrams, data-flow maps, trust boundaries, threat models, ADR index, API/event contracts, dependency/SBOM inventory.
- Explicit mapping between ten canonical agent profiles, eight Agentic DevOps lifecycle roles, 20 mined knowledge profiles, and six runtime worker roles. Similar names must not be silently merged.
- Versioned agent registry and tool capability registry.
- Architecture review for new data stores, model providers, network paths, privileged tools, and external integrations.
- SLOs and error budgets for RAG, MCP, model gateway, orchestration, and deployment.

Acceptance:
- 100% of production services have owner, dependency map, threat model, SLO, operational runbook, and support tier.
- 100% of agent runtime identities map to a reviewed canonical profile and explicit capability grant.
- No undocumented privileged integration or unowned critical dependency.

### 4.3 RAG and data governance

Preserve the current PostgreSQL/pgvector and hybrid vector/full-text RRF baseline initially. Do not add a new vector store or reranker without measured evidence of need.

Deliver:
- Source registry with owner, tenant, repository/project scope, classification, lawful-use/license basis, canonical URI, version, SHA-256, parser version, retention/deletion rule, and verification status.
- Type-aware ingestion for Markdown, code, API schemas, OpenAPI, YAML/TOML, SQL migrations, test evidence, approved runbooks, and model documentation.
- Secret/PII detection and quarantine before embedding.
- Stable source locators: repository commit, file path, symbol/heading, and line range.
- Incremental commit-aware repository indexing, tombstones for deleted files, branch/PR isolation, release snapshots, and stale-index detection.
- Explicit distinction among PROPOSED, UNVERIFIED, IMPLEMENTED, TESTED, DEPLOYED, SUPERSEDED, and REJECTED knowledge.
- Human-reviewed gold sets for shared architecture, each supported agent domain, and repository engineering tasks.
- Data subject/source deletion workflow, retention enforcement, backup lifecycle, and index rebuild procedure.

Acceptance:
- 100% of indexed chunks have valid provenance, content hash, scope, source version, and verification status.
- Zero cross-tenant, cross-agent, or cross-project leaks in the complete negative authorization matrix.
- Re-ingestion is idempotent; changed sources preserve auditable history; deleted content is not returned as current.
- Recall@5 >= 0.95 and NDCG@10 >= 0.90 on frozen, human-reviewed, leakage-free gold sets; report confidence intervals and domain breakdown.
- Citation/source correctness >= 0.98 on independently adjudicated benchmark.
- 100% of retrieval requests disclose stale or incomplete source state where relevant.
- Retention/deletion tests verify removal from active index and documented handling of backups/history according to policy.

### 4.4 MCP, tool security, and agent identity

Deliver:
- Workload identity and short-lived, audience-bound, task-scoped grants.
- Authenticated principal fields: issuer, audience, subject, tenant, canonical agent/profile version, task/run, lease epoch, repository/project/environment, scopes, tool allowlist, expiry, delegation chain.
- mTLS for service-to-service channels where applicable.
- Separate read and write capability servers; Knowledge MCP remains read-only.
- Named tools with strict schemas; no generic arbitrary shell, arbitrary filesystem, arbitrary SQL, or unrestricted HTTP.
- Sandboxed engineering execution with fixed workspace, resource limits, timeouts, network egress allowlist, secret isolation, output redaction, and artifact capture.
- Idempotency keys, operation ledger, deadline propagation, bounded retry, reconciliation, cancellation/kill switch, DLQ and audited redrive.
- Tamper-evident audit records with correlation IDs and principal/resource/policy/result metadata.
- Independent penetration tests and prompt-injection/tool-abuse test suites.

Acceptance:
- 100% of forbidden tenant/agent/project/tool/environment combinations are denied before invocation.
- Zero unauthorized side effects across adversarial and negative authorization tests.
- 100% of side-effect attempts and policy decisions produce complete correlated audit records.
- Duplicate operation replay creates at most one side effect; ambiguous timeout triggers reconciliation, not blind retry.
- P0 cancellation p95 <= 2 seconds under documented design load, if retained as the approved ADR target.
- Zero path traversal, unapproved network egress, secret leakage, or unbounded execution in sandbox tests.
- No production write/deploy capability by default.

### 4.5 Model training, registry, and lifecycle

Hybrid strategy:
- Approved base models provide general reasoning.
- RUSA fine-tuned adapters improve role-conditioned behavior, structured outputs, task decomposition, safe tool selection, and recovery.
- Centralized RAG supplies changing factual/project knowledge.
- Deterministic policy/tool gateway controls all real permissions.

Deliver:
- Dataset registry: schema version, source, license/lawful basis, consent/authorization where required, classification, redaction, annotator/reviewer, split lineage, quality status, content hash.
- Separate training, validation, test, red-team, and regression sets; repository/scenario/time disjointness to prevent leakage.
- Human-reviewed examples from verified engineering tasks: plan, code, build, test, debug, repair, refactor, dependency update, release, operate, monitor, blocked actions, retries, cancellation, and handoffs.
- Teacher-model workflow using approved APIs/providers only where contract, privacy, retention, and data-use settings permit. Teacher outputs are candidates, not ground truth; independently verify code and labels. Do not claim access to proprietary ChatGPT weights.
- Reproducible SFT/QLoRA runs with pinned base revision, tokenizer, dataset hashes, code, environment, hyperparameters, hardware, seed, and training logs.
- Model registry with immutable artifacts, signed/checksummed manifests, evaluation reports, approvals, deployment stages, and rollback.
- Model/provider fallback policy, cost/latency controls, drift monitoring, and periodic re-evaluation.

Acceptance:
- 100% dataset records validate against versioned schema and have provenance/review status.
- Zero known train/eval/test leakage across content, scenario, repository, and source lineage.
- 100% of model artifacts pass signature/checksum and manifest verification before loading.
- 100% held-out structured response/tool-contract validity.
- Blocked-action recall >= 0.90 and blocked false-positive rate <= 0.10, with per-role results.
- Staging task success >= 0.80 on a human-reviewed held-out engineering benchmark, then raise target based on measured baseline and risk tier.
- Zero unauthorized tool invocation in adversarial tests.
- No release-blocking regression against the prior certified model.
- Model rollback rehearsal succeeds and restores prior known-good model.
- No model is promoted solely because training loss decreases.

### 4.6 Independent validation and QA

Deliver:
- Independent QA role that did not author the change.
- Test pyramid: unit, contract, integration, system, security, performance, reliability, acceptance, and adversarial tests.
- Frozen benchmark tasks for create-project, add-feature, repair-bug, update-dependency, refactor, incident response, and rollback.
- Reproducible test environment and immutable test report tied to commit and artifact digest.
- Defect severity and release-blocking policy.

Acceptance:
- 100% of mandatory release tests pass on exact release commit.
- Zero unresolved critical/high security findings without formally approved time-bound exception and compensating control.
- Zero unauthorized side effects.
- Engineering task success >= 0.80 on initial held-out staging benchmark, with per-task results and confidence intervals.
- Regression rate and escaped-defect rate reported each release.
- QA approval is independent and cannot be self-issued by the authoring agent.

### 4.7 Operational resilience and reliability

Deliver:
- SLOs, service health dashboards, alerting, on-call ownership, runbooks, capacity plans, spend budgets, and vendor failure handling.
- Tested backups, restore, disaster recovery, key rotation, secrets recovery, and dependency outage procedures.
- Incident severity matrix, escalation, containment, communication, evidence preservation, and postmortems.
- Canary/blue-green release, rollback, feature flags, kill switch, and safe degradation when model/RAG/MCP dependencies fail.
- Business continuity and disaster recovery exercises.

Acceptance:
- Every tier-1 service has approved SLO, runbook, owner, backup/restore objective, and alert test.
- Backup restore and disaster recovery drills pass at the approved RPO/RTO at least twice yearly.
- Rollback tested for every production release class.
- No critical alert without owner and actionable runbook.
- Cost and rate limits enforced for all model and tool workloads.

### 4.8 Compliance, privacy, and audit

Deliver:
- Control mapping to applicable ISO/IEC 42001, ISO/IEC 27001, NIST AI RMF, privacy/legal obligations, and relevant secure-development guidance.
- Data inventory and flow diagrams, purpose limitation, retention, deletion, access review, supplier/subprocessor inventory, cross-border transfer review where applicable.
- Evidence repository with immutable references to control, test, approval, incident, exception, and release.
- Internal audit schedule and independent external assessment where conformity/certification is sought.
- Corrective action tracking with owner, due date, root cause, retest, and closure approval.

Acceptance:
- 100% applicable controls have owner, evidence, review date, and status.
- Quarterly access reviews for privileged identities and production data.
- No overdue critical/high audit findings.
- Privacy/security impact assessments completed for applicable high-risk data and use cases.
- Any certification or conformity claim is made only by the competent independent assessor/certification body.

## 5. Phased execution plan

### Phase 0 — Baseline, ownership, and threat model
- Freeze the system inventory and canonical identity mapping.
- Verify repository baseline, draft PR state, existing migrations, CI and security findings.
- Create RACI, risk register, control applicability matrix, and architecture threat model.
- Assign owners and evidence retention.

Exit: every critical system, data flow, agent identity, and privileged capability has an owner and scope.

### Phase 1 — Complete governed RAG
- Preflight four owner-provided source documents using pinned SHA-256 manifest.
- Review and ingest 20 mapped agent packs into an authorized non-production tenant.
- Implement source registry, codebase indexing, incremental refresh, tombstones, retention, and deletion.
- Build frozen human-reviewed gold sets.

Exit: RAG acceptance tests pass, metrics published, zero scope leakage.

### Phase 2 — Agent-aware MCP and security
- Implement trusted workload/agent principal propagation.
- Enforce task-bound project/repository/environment grants.
- Separate read/write capabilities and named tools.
- Complete idempotency, audit, cancellation, sandbox, penetration and prompt-injection tests.

Exit: MCP acceptance tests pass; no production write permission by default.

### Phase 3 — Governed model lifecycle
- Expand SFT data with reviewed engineering evidence.
- Use approved teacher/evaluator providers only under approved data handling terms.
- Run reproducible QLoRA experiment, benchmark, red-team, and model registry promotion.
- Add rollback and model monitoring.

Exit: model acceptance tests pass and staging candidate approved.

### Phase 4 — Autonomous engineering validation
- Test create-project, feature development, bug repair, dependency update, refactor, incident remediation, and rollback.
- Use isolated branches/worktrees and sandboxed tools.
- Bind QA results to exact commits and artifacts.
- Require approval for protected merge, production deployment, destructive operations, and high-risk changes.

Exit: held-out benchmark target met; zero unauthorized side effects; no release-blocking regression.

### Phase 5 — Production operations and independent assurance
- Operate within approved risk tier and SLOs.
- Complete restore/DR drills, access reviews, internal audit, and external independent assessment where required.
- Close findings and demonstrate sustained operating effectiveness.

Exit: sustained evidence over an agreed audit period; no unresolved critical/high control failure; independent assurance sign-off.

## 6. Release gates

A release cannot advance merely because CI is green.

- **Gate A — Data:** source rights, provenance, classification, secret scan, RAG scope, and freshness pass.
- **Gate B — Identity/security:** authenticated principal, tool allowlist, scope isolation, threat tests, and audit pass.
- **Gate C — Model:** reproducible artifact, frozen benchmark, red-team, approval, and rollback pass.
- **Gate D — Engineering:** build, test, security scan, SBOM, QA, and exact commit/artifact binding pass.
- **Gate E — Operations:** SLO, monitoring, runbook, capacity, restore/rollback, and incident readiness pass.
- **Gate F — Governance:** accountable owner and required human approvals recorded.

A failed gate blocks promotion. Exceptions are explicit, time-bound, approved, and visible.

## 7. Institutional scorecard and evidence requirements

For every pillar record:
- score and maturity anchor;
- control IDs and applicability;
- evidence URI/hash and exact commit/release;
- test date, sample size, benchmark version, and result;
- operating period and review frequency;
- owner and independent reviewer;
- exceptions, risk acceptance, due date, and remediation status.

A 10/10 score requires sustained operating effectiveness over an agreed period (recommended minimum 6–12 months for critical production controls), independent audit, successful resilience exercises, and no unresolved critical/high findings. A design document, a green CI run, or a one-time demo cannot satisfy this threshold.

## 8. Current baseline limitations to close

Based on the repository state documented in the Hybrid Intelligence plan and draft PR #82:
- Four source documents and 20 mapped knowledge profiles have a seed/mining workflow, but production ingestion is not evidenced.
- Standalone Knowledge MCP remains shared-only where authenticated agent principal is unavailable.
- Six runtime worker roles and 20 documented profiles require explicit reviewed mapping; they are not interchangeable.
- QLoRA pipeline exists, but current 120/36 synthetic seed is not a sufficient institutional engineering dataset and training has not been evidenced as completed.
- Teacher-model policy does not provide access to proprietary ChatGPT weights; API outputs require contractual/data-handling review and independent validation.
- Gold sets and semantic engineering benchmarks need human review and frozen evaluation.
- Draft PR CI success does not establish production security, operational resilience, compliance, or autonomous engineering readiness.
- Existing container scan findings and other security findings must be triaged to closure or formally risk-accepted before production.

## 9. Immediate next actions

1. Approve this program's control owners, risk tiers, and audit scope.
2. Close RAG source preflight and non-production ingestion with reviewed provenance.
3. Complete the canonical agent/runtime identity mapping.
4. Implement authenticated agent-aware MCP and negative authorization tests.
5. Build human-reviewed RAG gold sets and engineering task benchmarks.
6. Expand and review SFT candidate data; run training only after data governance gates pass.
7. Complete independent security, reliability, and privacy assessments.
8. Run autonomous engineering benchmark in isolated staging.
9. Establish recurring access reviews, restore drills, incident exercises, and internal audits.

**Final rule:** RUSA AI is rated 10/10 only when every pillar has independently verified operating evidence and no critical control failure. Until then, report the measured score and remaining gaps honestly.
