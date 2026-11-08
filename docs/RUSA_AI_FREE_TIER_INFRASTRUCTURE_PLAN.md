# RUSA AI Free-Tier Infrastructure Plan

**Status:** Proposed cost-minimized development and validation topology  
**Goal:** Build RUSA AI RAG, MCP, model-training and autonomous-engineering capabilities using legitimate free tiers where practical.  
**Important:** Free tiers are quotas, not production SLAs. This plan does not claim that the services have been provisioned or connected.

## 1. Design principles

1. Use free tiers for development, demos, small-scale evaluation, and intermittent training only.
2. Keep durable source-of-truth data in persistent storage, not Codespaces/Kaggle/Colab ephemeral disks.
3. Never place production secrets, sensitive customer data, private repository source, or regulated data in public datasets/spaces.
4. Enforce tenant/agent/project isolation in RUSA application code and database policies; a free provider plan is not a substitute for security design.
5. Set usage alerts/limits where offered; verify provider terms and current quotas before each deployment.
6. Do not promise always-on inference, high availability, guaranteed GPU, or enterprise compliance on free tiers.
7. Keep an export/restore path so the architecture can migrate to paid infrastructure without redesign.

## 2. Recommended free-tier stack

| RUSA capability | Preferred free-tier service | Intended use | Main constraint / fallback |
|---|---|---|---|
| Source control | GitHub Free | Private source repository, PR review, issues | Codespaces quota is limited; CI minutes/large artifacts are bounded |
| Development environment | GitHub Codespaces personal free quota | Backend, MCP, tests, migrations, orchestration development | 120 core-hours and 15 GB-month storage for personal Free accounts; use only while actively developing |
| RAG database | Supabase Free PostgreSQL + pgvector | Non-production knowledge index and metadata | 500 MB database, 1 GB file storage, 5 GB egress; free projects pause after 1 week inactivity; not a production HA database |
| Object storage | Cloudflare R2 Standard free tier | RAG source archives, small artifacts, model adapters/checkpoints | 10 GB-month, 1M Class A, 10M Class B operations; egress free; larger model/base-weight storage exceeds free allowance |
| Dataset/model artifact registry | Hugging Face Hub | Public non-sensitive datasets/models; private development artifacts within free account limits | Free public storage is best-effort; private storage has a 100 GB free limit; do not treat as guaranteed enterprise backup |
| GPU training | Kaggle Notebooks | Intermittent QLoRA experiments and evaluation | Free GPU quota is limited (documented as around 30 hours/week, subject to change/availability); sessions are ephemeral |
| GPU training fallback | Google Colab Free | Short experiments if Kaggle unavailable | GPU availability and usage limits fluctuate; runtime is not guaranteed; max runtime generally up to 12 hours depending on availability |
| Local/CPU inference | Codespaces or developer machine | API wiring, small quantized models, contract tests | Not suitable for high-throughput 4B model inference on CPU; latency depends on allocated CPU/RAM |
| Monitoring/evidence | GitHub Actions artifacts + repository docs | Test reports, benchmark summaries, provenance manifests | Retention and storage limits apply; do not store secrets or large model checkpoints as CI artifacts |

Official quota references should be checked at deployment time:
- GitHub Codespaces: https://docs.github.com/en/billing/concepts/product-billing/github-codespaces
- Supabase billing: https://supabase.com/docs/guides/platform/billing-on-supabase
- Cloudflare R2 pricing: https://developers.cloudflare.com/r2/pricing/
- Hugging Face storage limits: https://huggingface.co/docs/hub/storage-limits
- Kaggle GPU usage: https://www.kaggle.com/docs/efficient-gpu-usage
- Google Colab FAQ: https://research.google.com/colaboratory/faq.html

## 3. RUSA target topology

### Development/control plane
- GitHub repository remains the source of truth.
- Codespaces runs the API, agent orchestrator, MCP client/server, test suite, and local development dependencies.
- Use Docker Compose locally/Codespaces for reproducible service wiring.
- Do not use Codespaces as an always-on public production server.

### RAG data plane
- Supabase PostgreSQL + pgvector hosts non-production metadata, chunks, and embeddings.
- Keep original source files and larger archives in R2 or an authorized private Hugging Face repository.
- Record source SHA-256, source version, parser version, chunk locator, classification, tenant/project/agent scope, and verification status.
- Run RAG ingestion in bounded jobs. Ingest the four owner-approved source documents and 20 profile packs only after source hash preflight, secret scan, and scope review.
- For small free-tier DB, start with architecture and agent profiles; defer full repository indexing until measured storage permits.
- Keep an exportable SQL/JSONL snapshot and test restore.

### Model data plane
- Existing 120/36 synthetic SFT seed remains a baseline, not production-quality training truth.
- Keep the additional 48 engineering examples in a separate candidate dataset until human-reviewed and schema-validated.
- Use Kaggle GPU or Colab Free for short, reproducible QLoRA experiments only.
- Pull pinned base model weights from the model publisher when training; do not duplicate large base weights into Git.
- Store adapter/checkpoint manifests and small adapter artifacts in private Hugging Face Hub or R2 within quota. Store larger checkpoints temporarily in notebook storage only during the run, then export immediately.
- Record base revision, dataset hashes, training code commit, hyperparameters, hardware, seed, and evaluation report.
- Do not train on unreviewed production traces, confidential source code, secrets, or evaluation-set examples.

### Inference plane
- Initial free stage: keep model inference optional and intermittent. Use CPU/quantized inference for small smoke tests; use a permitted hosted model API only behind a provider abstraction and policy controls if a free quota is available.
- Do not assume Kaggle or Colab can be used as an always-on inference endpoint; notebook runtimes are temporary and governed by interactive-use policies.
- When the RUSA adapter is certified, deploy it to a dedicated inference host only when persistent uptime and performance are required. This may require paid compute.
- Keep RAG and MCP functional with a mock/model-provider interface so development can proceed before a trained adapter exists.

## 4. Cost-minimized implementation phases

### Phase A — Zero-cost foundation
1. Keep source code and implementation plans in GitHub.
2. Develop/test in Codespaces within personal monthly quota; stop unused Codespaces.
3. Start Supabase Free for a non-production database and enable pgvector.
4. Use R2 Standard for small source bundles and model artifacts within the free quota.
5. Add quota/usage monitoring and a documented export/backup process.
6. Run unit/contract/security tests without a GPU.

Exit: application starts reproducibly; migrations apply; DB connectivity works; secrets are externalized; no data is stored only on ephemeral disks.

### Phase B — RAG pilot
1. Create a dedicated non-production Supabase project.
2. Apply RUSA migrations and verify tenant RLS with positive and negative tests.
3. Preflight source manifest and all four source hashes.
4. Ingest four sources and 20 agent packs in small batches.
5. Measure DB growth, embedding cost/quota, retrieval quality, and source provenance.
6. Export and restore a database snapshot.

Exit: ingestion idempotent; source lineage complete; no cross-tenant/agent/project leakage; benchmark is recorded. If the DB quota is exceeded, stop ingestion and migrate to a larger persistent PostgreSQL service rather than silently dropping data.

### Phase C — SFT dataset quality
1. Validate existing seed JSONL against repository schema.
2. Keep the 48-example candidate separate; human-review every record.
3. Add only authorized, redacted, verified engineering examples.
4. Create a separate frozen held-out evaluation set with no scenario/repository leakage.
5. Record dataset version and checksums.

Exit: every promoted example has provenance and review status; no eval leakage; safety/blocked-action coverage reported.

### Phase D — QLoRA experiment
1. Pin base model revision and training environment.
2. Run a short smoke test on Kaggle/Colab before full run.
3. Save checkpoints at bounded intervals and export them before runtime expiry.
4. Evaluate on frozen holdout and adversarial cases.
5. Keep candidate adapter unpromoted until task-level benchmarks pass.

Exit: reproducible manifest, valid adapter, evaluation report, and rollback/reference to previous model. A completed training process alone is not acceptance.

### Phase E — Autonomous engineering pilot
1. Run in isolated Codespaces/devcontainer workspaces.
2. Give agents only scoped repository/project grants.
3. Use named MCP tools; no unrestricted shell or public write credentials.
4. Require tests, security checks, and independent QA on exact commit.
5. Keep merge, deployment, destructive migrations, secret/policy changes behind approval.

Exit: benchmarked project creation/repair/update with zero unauthorized side effects and evidence tied to commit.

## 5. Free-tier resource budgeting

- Keep the initial RAG corpus compact: four source docs and 20 profile packs first.
- Do not index all repositories or large binary files before measuring DB size and retrieval value.
- Store source files in object storage; store chunks/metadata/embeddings in Postgres.
- Avoid committing model weights, checkpoints, datasets with sensitive content, or DB dumps to Git.
- Use adapter-only export rather than full model merge when the inference stack supports it.
- Delete temporary notebook checkpoints after successful export and verify checksum before deletion.
- Set hard usage budgets and stop jobs before provider quota exhaustion.
- Keep a local encrypted backup of critical manifests and source documents.

## 6. Security and governance restrictions

- Supabase Free is for non-production pilot only until backup, recovery, availability, and security requirements are independently assessed.
- Public Hugging Face repositories may expose all content; never upload private source, user data, secrets, or proprietary training examples publicly.
- Private Hub storage and R2 quotas are not equivalent to enterprise backup, retention, legal hold, or compliance controls.
- Notebook providers are compute environments, not trusted long-running servers. Do not host MCP endpoints or expose long-lived credentials from Kaggle/Colab.
- Codespaces port forwarding must be private/authenticated. Never expose database, MCP admin, or model-control ports publicly without gateway authentication.
- Free-tier availability, limits, terms, and regions may change. Recheck provider documentation and account dashboards before use.
- Maintain a migration plan to paid services when usage, security, reliability, or compliance exceeds free-tier capabilities.

## 7. Acceptance tests

| ID | Test | Acceptance |
|---|---|---|
| FREE-01 | Clean environment setup | New Codespace builds from repository instructions without undocumented manual state |
| FREE-02 | Secrets | No secrets committed; secret scan passes; runtime secrets injected securely |
| FREE-03 | Database | Supabase project connects, pgvector extension and RUSA migrations work |
| FREE-04 | RLS | Cross-tenant, agent, and project negative tests all deny access |
| FREE-05 | Ingestion | Four source hashes match manifest; 20 packs parse; ingestion is idempotent |
| FREE-06 | Provenance | Every indexed chunk has source/version/hash/locator/scope/status |
| FREE-07 | Storage | Database and object usage remain under quota with alert threshold at 80% |
| FREE-08 | Backup/restore | Export can be restored into a clean non-production project and checksums match |
| FREE-09 | Dataset | Baseline and candidate files validate; candidate remains separate until reviewed |
| FREE-10 | Training | Kaggle/Colab smoke test completes, saves checkpoint, exports artifact, verifies checksum |
| FREE-11 | Model evaluation | Frozen held-out evaluation and blocked-action metrics recorded before promotion |
| FREE-12 | MCP | No notebook environment hosts privileged MCP; agent grants are scoped and short-lived |
| FREE-13 | Autonomous pilot | Changes occur only in isolated workspace/branch; mandatory tests and QA pass |
| FREE-14 | Quota exhaustion | Service degrades safely; no silent data loss; migration/export procedure works |

## 8. What cannot realistically be free forever

A dependable 24/7 autonomous engineering platform with persistent database, large private model storage, dedicated GPU inference, backups, observability, support, and enterprise-grade availability cannot responsibly be promised at zero cost.

The free-tier design is intended to build and validate the architecture. Before production, budget for the components that need guaranteed uptime, stronger backup/restore, larger database/storage, dedicated inference, security assurance, and compliance controls.

**Decision:** Start with GitHub + Codespaces + Supabase Free + Cloudflare R2 + Hugging Face Hub + Kaggle/Colab GPU. Keep all data exportable and provider-independent. Upgrade only when measured quotas or institutional controls require it.
