---
title: "RusaAi Repository Roadmap"
document: "rusaai.md"
repository: "StrategyG/RusaAi"
version: "1.3.0"
updated: "2026-11-12"
status: "Repository engineering roadmap"
language: "id"
---

# Roadmap Pengembangan Repository RusaAi

> Dokumen ini mengatur pekerjaan engineering di repository `StrategyG/RusaAi`: struktur kode, kontrak, API, agent runtime, queue/scheduler, knowledge, security, deployment, CI/CD, dan release. Ini bukan roadmap umum pengembangan produk atau program training model.

## 1. Tujuan dan batas ruang lingkup

Tujuan roadmap adalah membuat repository RusaAi konsisten, dapat diuji, aman, mudah dipelihara, dan siap dirilis secara bertahap.

### Termasuk dalam roadmap

- Struktur dan batas tanggung jawab modul repository.
- FastAPI gateway dan inference provider.
- `rusa_agents`: orchestration, workers, task queue, policy, tools, memory, audit, metrics.
- `rusa_knowledge` dan `rusa_mcp`.
- Database migrations, tenant isolation, schema, dan shared contracts.
- CPU inference integration, GGUF verification, model manifest, dan runtime lifecycle.
- Docker Compose, sandbox, CI/CD, security scan, observability, backup/restore, dan release.
- Pengujian integrasi lintas modul dan acceptance test repository.

### Di luar ruang lingkup roadmap ini

- Program riset/training neural network secara mandiri.
- Kurasi dan pengumpulan dataset model sebagai workstream terpisah.
- Klaim bahwa model RusaAi-Edge sudah tersedia atau siap produksi.
- Pengembangan produk/aplikasi eksternal yang tidak berada di repository ini.

Model hanya dibahas sejauh repository perlu mengidentifikasi artifact, memvalidasi manifest/checksum, menjalankan provider, dan menguji kontrak inference. Tidak boleh memasukkan bobot model atau file GGUF privat ke Git.

## 2. Sumber acuan repository

| Sumber | Cara digunakan |
|---|---|
| `RusaAi_Architecture_v2.0.md` | Acuan batas Control Plane/Model Plane, inference, workflow, memory, security, observability, deployment, dan acceptance |
| `RusaAi_Training_Engine_v1.0.md` | Hanya bagian yang berdampak pada interface repository: model manifest, tokenizer/model metadata, artifact handoff, evaluation interface |
| `RUSA_AI_CPU_Deployment_Configuration_v1.0.md` | Acuan konfigurasi dan operasi CPU yang perlu diwujudkan dalam Compose, env, runtime, dan tests |
| `RUSA_AI_All_Agents-2.md` | Acuan role, permission, input/output, workflow, dan communication contract agent |
| `MASTER_CONTEXT.md` | Acuan tata kelola perubahan dan kesinambungan dokumentasi |

Jika dokumen sumber berbeda versi atau saling bertentangan, jangan menggabungkan nilainya secara diam-diam. Buat ADR, jelaskan keputusan, dan pertahankan identitas konfigurasi lama yang masih dipakai.

## 3. Snapshot repository

Snapshot terakhir yang diverifikasi saat dokumen ini diperbarui:

| Item | Status |
|---|---|
| Repository | `StrategyG/RusaAi` |
| Default branch | `main` |
| `main` commit | `31a2bb74aaccaa6d3f5c8b1f0379df6c5d4bda7d` |
| Working branch | `feat/architecture-v2-cpu-first-alignment` |
| Branch head | `5ecaf9cd90837ff5c30b75440d3b06452a9ee19b` |
| Pull request | [#107](https://github.com/StrategyG/RusaAi/pull/107) — open, belum merge |
| CI run terbaru pada snapshot | [Run #4221](https://github.com/StrategyG/RusaAi/actions/runs/43071904195) |
| Status Run #4221 | Dua job selesai sukses; image/sandbox job masih berjalan saat pemeriksaan |
| `main` | Tidak diubah oleh roadmap ini |

Status CI bersifat per-commit. Periksa ulang GitHub Actions sebelum menyatakan PR lulus atau merge. Run #4220 pada commit sebelumnya lulus, tetapi itu bukan pengganti hasil Run #4221 pada head terbaru.

## 4. Peta struktur repository dan ownership

| Path | Tanggung jawab repository | Status ringkas |
|---|---|---|
| `Rusaai/` | Penanda batas ownership Control Plane | README/architecture marker; belum menjadi lokasi fisik seluruh implementasi |
| `RusaModels/` | Penanda batas ownership Model Plane | README/architecture marker; belum menjadi full model registry/runtime package |
| `rusa_agents/` | API support, orchestrator, workers, queue, policy, tools, memory, audit, provider, metrics | Fondasi aktif |
| `rusa_knowledge/` | Parsing, chunking, embeddings, retrieval, store, evaluation | Fondasi aktif |
| `rusa_mcp/` | MCP client/server | Fondasi aktif |
| `llm-project/serve/` | FastAPI serving dan model runtime | Implementasi aktif; kontrak v2 perlu integrasi penuh |
| `llm-project/model/` | Blueprint dan konfigurasi referensi model | Blueprint tersedia; bukan bukti checkpoint tersedia |
| `shared/contracts/` | Agent, artifact, dan inference contracts | Kontrak awal tersedia; consumer integration perlu diverifikasi |
| `schemas/` | Schema A2A dan LLM yang sudah ada | Dipakai oleh runtime |
| `migrations/` | Task, lease, tenant, audit, knowledge migrations | Fondasi aktif; migration compatibility harus dijaga |
| `scripts/` | Artifact verification, manifest, benchmark dan maintenance scripts | Fondasi tersedia |
| `tests/` | Unit, integration, contract, security, migration, runtime, compose tests | Regression suite aktif |
| `docs/` | Runbooks, ADR, implementation map, readiness docs | Harus dijaga sinkron dengan kode |
| `.github/workflows/` | CI dan workflow repository | CI aktif |
| `docker-compose*.yml`, `Dockerfile*` | API, worker, MCP, sandbox, CPU profiles | Beberapa jalur deployment tersedia; acceptance perlu dilengkapi |

**Aturan migrasi struktur:** jangan memindahkan seluruh package ke `Rusaai/` atau `RusaModels/` hanya untuk menyamakan nama folder dengan diagram. Pertahankan lokasi saat ini sampai import graph, Docker build context, migrations, scripts, tests, dan deployment paths memiliki migration plan yang dapat direview dalam satu perubahan terukur.

## 5. Status implementasi repository saat ini

Status di bawah mengikuti implementation alignment dan file yang ada pada branch snapshot. “Tersedia” tidak otomatis berarti production-verified.

| Area | Kondisi saat ini | Pekerjaan tersisa |
|---|---|---|
| FastAPI inference gateway | Tersedia | Hubungkan shared inference schema, model registry lookup, deadline, dan error semantics |
| AICode orchestration | Enam specialist worker roles tersedia | General-purpose ten-agent orchestration dan role registry belum lengkap |
| Durable task queue | PostgreSQL task/lease, retry, idempotency, tenant controls tersedia | Perlu end-to-end DAG/deadline/cancellation/recovery validation |
| Inference admission | FIFO bounded API queue; active/waiting gauge, queue latency, dan rejection metrics tersedia | Masih process-local; belum menjadi shared cross-process queue untuk API dan worker |
| llama.cpp CPU | Compose integration dan satu server slot tersedia | Artifact selection/manifest gate dan device acceptance tetap diperlukan |
| Model artifact verification | GGUF verifier dan manifest scripts tersedia | Pin provenance/revision/digest ke runtime deployment dan registry lifecycle |
| Inference contracts | Versioned request/response JSON Schema tersedia | Belum sepenuhnya wired ke public endpoint dan worker |
| Knowledge/RAG | Tenant-scoped ingestion/retrieval dan evaluation tersedia | Lifecycle memory lima kategori dan end-to-end evidence/citation QA belum lengkap |
| MCP | Read-only knowledge MCP tersedia | Uji permission, token lifecycle, audit, rate limit, dan recovery |
| Tool security | Policy, allowlist, sandbox, audit foundations tersedia | Adversarial tenant isolation, prompt injection, network/filesystem restrictions, approval gates |
| Observability | API/worker metrics; API inference active/waiting, queue latency/rejections, generation duration, stream TTFT, provider errors | Model RAM, minimum host free RAM, CPU utilization, token throughput, saturation/error budget, serta worker-to-model telemetry |
| Deployment | Linux Docker/CPU profiles tersedia | Compose acceptance, host RAM check, backup/restore, Windows/Android evidence |
| CI/CD | Lint, tests, security, image build/scan workflow tersedia | Pastikan isolated test dan seluruh image/compose checks lulus di head PR |
| Release readiness | Dokumen readiness/certification tersedia | Staging soak, recovery drill, operator approval, signed release evidence |

## 6. Urutan roadmap repository

### Fase R0 — Baseline dan konsolidasi PR

**Prioritas: P0 · Status: IN_PROGRESS**

Pekerjaan:
- [ ] Review PR #107 dari base sampai head; pastikan hanya perubahan yang sesuai dengan architecture alignment.
- [ ] Periksa branch divergence, konflik, file overlap, dan dependency dengan PR lain sebelum merge.
- [ ] Pastikan hasil CI terbaru pada head PR selesai sukses.
- [ ] Pastikan test sandbox berjalan pada snapshot repository yang sama dengan workflow CI.
- [ ] Review README, implementation alignment, ADR, Compose, env example, dan tests agar menyatakan status yang sama.
- [ ] Tetapkan satu issue/PR untuk setiap gap P0; hindari satu PR besar yang mencampur semua subsistem.

Deliverable:
- PR #107 siap review/merge.
- Daftar gap repository yang terurut dan memiliki owner.
- Snapshot branch/CI yang dapat direproduksi.

Exit criteria:
- CI head terbaru hijau.
- Tidak ada perubahan tanpa test atau alasan pengecualian.
- Tidak ada konflik model identity, CPU env, Compose profile, atau schema version yang belum dicatat.

### Fase R1 — Repository contracts dan API boundary

**Prioritas: P0 · Status: PARTIAL**

Path utama:
- `llm-project/serve/app.py`
- `llm-project/serve/model_runtime.py`
- `rusa_agents/providers.py`
- `rusa_agents/config.py`
- `shared/contracts/inference_contract/`
- `tests/test_inference_contracts.py`
- `tests/test_app.py`
- `tests/test_inference_runtime.py`

Pekerjaan:
- [ ] Validasi request/response terhadap schema version yang dipublikasikan.
- [ ] Tetapkan model ID hanya dari model registry/config yang disetujui; tolak model yang tidak dikenal.
- [ ] Samakan request ID, task ID, agent ID, tenant scope, trace ID, dan error envelope.
- [ ] Terapkan deadline, cancellation, timeout, usage, dan performance fields secara konsisten.
- [ ] Buat response schema kondisional untuk status queued/running/completed/failed/cancelled/timed out.
- [ ] Pastikan metrik yang belum diukur tidak diisi nilai nol palsu.
- [ ] Pertahankan backward compatibility; perubahan breaking memerlukan schema version baru.
- [ ] Tambahkan contract tests untuk public API dan provider adapter, bukan hanya schema-file tests.

Exit criteria:
- Endpoint dan worker benar-benar memakai contract validator.
- Contract test mencakup success, invalid input, unknown model, timeout, cancellation, queue full, dan provider failure.
- Dokumentasi API sama dengan response aktual.

### Fase R2 — Shared inference scheduler

**Prioritas: P0 · Status: NOT IMPLEMENTED END-TO-END**

Path utama:
- `rusa_agents/inference_admission.py`
- `rusa_agents/providers.py`
- `rusa_agents/worker_main.py`
- `rusa_agents/workers.py`
- `llm-project/serve/`
- `docker-compose.cpu.yml`
- `tests/test_inference_admission.py`
- `tests/test_provider_limits.py`

Pekerjaan:
- [ ] Jadikan satu scheduler sebagai pintu masuk seluruh pemanggil inference, baik API maupun worker.
- [ ] Hapus direct worker-to-llama.cpp bypass setelah jalur scheduler siap.
- [ ] Simpan queue state dan request ownership secara aman lintas proses.
- [ ] Tetapkan FIFO/priority policy, bounded waiting, queue timeout, deadline, cancellation, dan idempotency.
- [ ] Pastikan satu active generation untuk baseline CPU.
- [ ] Tangani worker crash, caller disconnect, lease expiry, duplicate submit, dan slot handoff race.
- [x] Instrument API queue wait, active/waiting count, timeout/rejection, stream TTFT, generation duration, dan provider errors.
- [ ] Extend inference telemetry to worker-to-model calls and add model RAM, CPU utilization, and tokens/second measurements.

Exit criteria:
- API dan worker memakai scheduler yang sama.
- Uji concurrency lintas proses membuktikan tidak ada generation paralel yang melampaui konfigurasi.
- FIFO/priority, queue-full, timeout, cancellation, restart, dan crash recovery lulus.
- Tidak ada slot leak atau double release.

### Fase R3 — Durable workflow dan agent runtime

**Prioritas: P1 · Status: BASELINE EXISTS**

Path utama:
- `rusa_agents/orchestrator.py`
- `rusa_agents/autonomous.py`
- `rusa_agents/runtime.py`
- `rusa_agents/queue.py`
- `rusa_agents/workers.py`
- `rusa_agents/contracts.py`
- `rusa_agents/policy.py`
- `migrations/`
- `tests/test_aicode_runtime.py`
- `tests/test_autonomous_worker.py`
- `tests/test_queue_races.py`
- `tests/test_postgres_backends.py`

Pekerjaan:
- [ ] Tetapkan task state machine dan transisi legal.
- [ ] Validasi DAG/dependency dan cegah deadlock parent-child.
- [ ] Terapkan non-blocking yield ketika parent menunggu child task.
- [ ] Satukan deadline, cancellation, retry/backoff, idempotency, lease fencing, dan recovery.
- [ ] Tetapkan agent registry: identity, capability, input/output contract, permission, model profile, timeout, dan owner.
- [ ] Pertahankan enam specialist roles yang sudah ada; perluas role baru secara bertahap sesuai kontrak, bukan sekadar menambah nama agent.
- [ ] Jadikan QA/evaluation sebagai tahap verifikasi terpisah dari agent pembuat output.
- [ ] Pastikan release/deploy/destructive operations tetap approval-gated.

Exit criteria:
- Workflow dapat restart dari durable state.
- Task tidak dieksekusi ganda akibat retry/restart.
- Setiap hasil menyimpan evidence, test result, unresolved issue, dan audit event.
- Agent tidak dapat menaikkan permission sendiri.

### Fase R4 — Database, tenant isolation, memory, RAG, dan MCP

**Prioritas: P1 · Status: BASELINE EXISTS**

Path utama:
- `migrations/`
- `rusa_agents/memory.py`
- `rusa_agents/audit.py`
- `rusa_knowledge/`
- `rusa_mcp/`
- `tests/test_postgres_rls.py`
- `tests/test_knowledge_*`
- `tests/test_mcp_*`
- `tests/test_migration_contract.py`

Pekerjaan:
- [ ] Jaga migration berurutan, repeatable, dan backward-compatible.
- [ ] Uji PostgreSQL RLS untuk read/write/update/delete lintas tenant.
- [ ] Tegaskan lifecycle, retention, provenance, access policy, dan deletion untuk working, episodic, semantic, procedural, dan project memory.
- [ ] Pisahkan memory internal task dari knowledge base tenant.
- [ ] Pastikan retrieval result memiliki source ID, metadata, tenant ID, dan provenance.
- [ ] Uji chunking, parser, embedding, retrieval ranking, citation correctness, dan ACL.
- [ ] Perketat MCP authentication, token rotation, scope, rate limit, audit, dan read-only guarantee.
- [ ] Sediakan backup/restore test untuk database dan knowledge index.

Exit criteria:
- Tenant A tidak dapat membaca atau memodifikasi data Tenant B pada seluruh endpoint dan MCP tool.
- Migration test dapat dijalankan dari database kosong dan versi sebelumnya.
- Retrieval menyertakan evidence yang dapat ditelusuri.
- Backup dapat dipulihkan dan diverifikasi.

### Fase R5 — Tool Gateway, sandbox, policy, dan security

**Prioritas: P1 · Status: BASELINE EXISTS**

Path utama:
- `rusa_agents/tool_gateway.py`
- `rusa_agents/agent_tools.py`
- `rusa_agents/git_workspace.py`
- `rusa_agents/policy.py`
- `rusa_agents/http_limits.py`
- `Dockerfile.agent-sandbox`
- `tests/test_tool_gateway.py`
- `tests/test_agent_tool_registry.py`
- `tests/test_worker_kill_switch.py`

Pekerjaan:
- [ ] Pertahankan satu enforcement point untuk semua tool calls.
- [ ] Validasi tool name, argument schema, tenant, repository, path, network, dan resource limit.
- [ ] Uji sandbox non-root, read-only filesystem, capability drop, seccomp/no-new-privileges, PID/CPU/memory/time limits.
- [ ] Cegah prompt injection mengubah policy atau memperluas izin tool.
- [ ] Wajibkan human approval untuk destructive, release, deployment, secret, dan permission changes.
- [ ] Pastikan secret tidak masuk prompt, log, memory, atau test fixture.
- [ ] Tambahkan adversarial tests untuk path traversal, symlink, command injection, SSRF, data exfiltration, dan cross-tenant access.
- [ ] Audit semua tindakan dengan actor, tenant, task, tool, decision, result, dan timestamp.

Exit criteria:
- Tidak ada jalur tool yang melewati gateway.
- Tidak ada aksi destruktif tanpa approval.
- Adversarial security tests lulus dan hasilnya terdokumentasi.
- Security scanner tidak memiliki temuan critical/high yang belum ditangani sesuai policy release.

### Fase R6 — Runtime operations, observability, dan CPU Compose

**Prioritas: P1 · Status: PARTIAL — API inference telemetry baseline implemented**

Path utama:
- `docker-compose.yml`
- `docker-compose.cpu.yml`
- `docker-compose.cpu-8gb.yml`
- `.env.example`
- `.env.cpu.example`
- `.env.cpu-8gb.example`
- `deployment/profiles/`
- `scripts/verify_gguf_model.py`
- `scripts/create_model_manifest.py`
- `scripts/benchmark_inference.py`
- `rusa_agents/metrics.py`
- `docs/CPU_INFERENCE_OPERATIONS.md`
- `tests/test_cpu_inference_compose.py`
- `tests/test_gguf_model_verifier.py`

Pekerjaan:
- [ ] Pastikan Compose base + CPU 16 GB + CPU 8 GB valid dan tidak memiliki env drift.
- [ ] Validasi konfigurasi efektif hasil merge, bukan hanya string di file env.
- [ ] Terapkan model artifact manifest dengan immutable revision, model ID, tokenizer/template metadata, quantization, runtime compatibility, dan SHA-256.
- [ ] Jangan commit model weights/GGUF; mount artifact read-only dari lokasi eksternal.
- [ ] Monitor host free RAM dan container memory; konfigurasi container limit tidak membuktikan host memiliki free RAM 2 GB.
- [ ] Tambahkan metrics queue wait, active/waiting slots, TTFT, tokens/sec, peak model RAM, provider errors, worker lease, dan task duration.
- [ ] Uji health/readiness, graceful shutdown, restart, dependency failure, and backpressure.
- [ ] Dokumentasikan perbedaan staging Compose dan production deployment boundary.

Exit criteria:
- `docker compose config` lulus untuk seluruh profile.
- GGUF checksum mismatch dan manifest mismatch ditolak.
- Tidak ada GPU offload pada CPU profile.
- Host free RAM minimal 2 GB selama load dan inference representatif.
- Metrics tersedia dan dapat dikorelasikan dengan request/task ID.

### Fase R7 — CI/CD, dependency hygiene, dan repository quality

**Prioritas: P0/P1 · Status: ACTIVE, perlu diperkuat**

Path utama:
- `.github/workflows/ci.yml`
- `.github/workflows/web-ci.yml`
- `.github/workflows/agent-sft-training.yml`
- `.github/workflows/benchmark-validation.yml`
- `pyproject.toml`
- `requirements*.txt`
- `Dockerfile*`
- `tests/`

Pekerjaan:
- [ ] Jalankan compile, Ruff, Bandit, pytest, dataset/contract validation, migration tests, dan coverage gate yang disepakati.
- [ ] Build semua production images dan sandbox image pada CI.
- [ ] Jalankan isolated full test suite di sandbox image.
- [ ] Validasi Compose production, CPU 16 GB, dan CPU 8 GB.
- [ ] Scan seluruh images dengan Trivy; tetapkan exception hanya dengan owner, expiry, dan alasan.
- [ ] Pin GitHub Actions dan base images; update dependency secara terkontrol.
- [ ] Pisahkan CI pull request, scheduled benchmark, dan workflow training agar tidak saling mengubah hasil.
- [ ] Upload test reports, coverage, security scan, dan image digests sebagai artifacts.
- [ ] Hindari job diagnostik sementara yang tidak mempunyai cleanup/retention policy.

Exit criteria:
- Semua required checks hijau pada commit yang akan di-merge.
- Tidak ada critical/high vulnerability tanpa approved exception.
- Image digest, test report, dan commit SHA dapat ditelusuri.
- CI dapat direproduksi dari clean checkout.

### Fase R8 — Repository release readiness

**Prioritas: P0 untuk setiap release · Status: NOT VERIFIED**

Pekerjaan:
- [ ] Lengkapi staging environment dan konfigurasi production yang aman.
- [ ] Jalankan integration, concurrency, load, soak, adversarial, tenant-isolation, and failure-recovery tests.
- [ ] Uji backup/restore database dan rollback schema/application.
- [ ] Verifikasi TLS, secret management, access policy, rate limits, logs, metrics, alerts, and operator runbook.
- [ ] Tetapkan release version, changelog, SBOM, image digests, schema versions, migration notes, and rollback procedure.
- [ ] Sediakan production sign-off yang menunjuk ke bukti uji, bukan hanya checklist.
- [ ] Publikasikan known limitations dan komponen yang belum didukung.

Exit criteria:
- Semua critical acceptance criteria lulus.
- Tidak ada open critical security/reliability issue.
- Restore dan rollback telah didemonstrasikan.
- Operator dapat deploy, monitor, diagnose, dan recover menggunakan runbook.

## 7. Urutan prioritas setelah PR #107

Urutan kerja repository yang direkomendasikan:

1. **Tutup review PR #107:** periksa CI Run #4221 pada head terkini, review diff, dan merge hanya setelah approval.
2. **Inference contract integration:** hubungkan `shared/contracts/inference_contract` ke endpoint dan provider.
3. **Shared inference scheduler:** hilangkan jalur direct worker-to-model yang melewati scheduler.
4. **Workflow recovery:** deadline, cancellation, lease fencing, idempotency, retry, DAG, dan restart.
5. **Tenant/security hardening:** RLS, MCP scopes, sandbox, approval, audit, dan adversarial tests.
6. **Memory/RAG integration:** lifecycle memory, provenance, retrieval evaluation, dan citation checks.
7. **Observability/operations:** queue, model, worker, host RAM, health, alert, backup/restore.
8. **CI/CD release gates:** seluruh image, isolated tests, Compose profiles, security scan, and artifact evidence.

Jangan mengerjakan ekspansi agent, folder migration besar, atau optimasi model sebelum P0 di atas stabil.

## 8. Definition of Done untuk perubahan repository

Setiap PR wajib memiliki:

- [ ] Scope dan alasan perubahan.
- [ ] Path/modul yang terdampak.
- [ ] Contract/schema compatibility statement.
- [ ] Unit/integration/regression tests.
- [ ] Security dan tenant impact assessment.
- [ ] Migration/rollback note jika database atau runtime config berubah.
- [ ] Dokumentasi/runbook yang diperbarui.
- [ ] CI result pada commit head PR.
- [ ] Evidence dan known limitations.
- [ ] Approval sesuai code ownership.

Sebuah komponen disebut:
- `DESIGN` jika baru ada dokumen atau diagram.
- `IMPLEMENTED` jika code path tersedia dan test relevan ada.
- `VERIFIED` jika acceptance tests dan evidence lulus.
- `RELEASED` jika version, artifact, migration, rollback, dan approval tersedia.

## 9. Aturan perubahan repository

1. Semua perubahan melalui branch dan pull request.
2. Jangan mengubah `main` secara langsung.
3. Jangan memindahkan package hanya untuk menyesuaikan diagram tanpa import/deployment migration plan.
4. Jangan mengubah kontrak v1 secara breaking; buat versi baru.
5. Jangan menyimpan secrets, dataset privat, tokenizer payload, model weights, atau GGUF privat di Git.
6. Model identity harus berasal dari manifest/artifact, bukan sekadar alias API.
7. Jangan menyatakan production-ready hanya karena pytest atau satu job CI lulus.
8. Perbarui dokumen ini jika scope, branch, acceptance gate, atau status implementasi berubah.
9. Catat keputusan arsitektur melalui ADR.
10. Setiap status `VERIFIED` harus merujuk pada commit, workflow run, test report, benchmark, atau acceptance evidence.

## 10. Referensi repository

- Repository: https://github.com/StrategyG/RusaAi
- Architecture PR #107: https://github.com/StrategyG/RusaAi/pull/107
- CI Run #4221: https://github.com/StrategyG/RusaAi/actions/runs/43071904195
- Implementation alignment: `docs/architecture/IMPLEMENTATION_ALIGNMENT.md`
- Autonomous implementation mapping: `docs/AUTONOMOUS_IMPLEMENTATION_ROADMAP.md`
- CPU operations: `docs/CPU_INFERENCE_OPERATIONS.md`
- Production readiness: `docs/PRODUCTION_READINESS.md`
- Production certification: `docs/PRODUCTION_CERTIFICATION.md`

---

## Change log

| Versi | Tanggal | Perubahan |
|---|---|---|
| 1.0.0 | 2026-11-12 | Roadmap awal mencakup keseluruhan proyek dan ekosistem. |
| 1.1.0 | 2026-11-12 | Dipersempit menjadi roadmap engineering repository RusaAi: modul, API, contracts, scheduler, agents, DB/RAG/MCP, security, deployment, CI/CD, dan release. |
| 1.2.0 | 2026-11-12 | Mencatat readiness fail-closed dan API inference telemetry; menandai batas metrics process-local serta pekerjaan observability worker/model yang tersisa. |
| 1.3.0 | 2026-11-12 | Menambahkan log implementasi R1: deadline API untuk queue dan generation, pembatalan completion/stream, metrik deadline, regression tests, serta daftar pekerjaan cross-process yang masih terbuka. |

## 11. Implementation progress — 2026-11-12

### R1 — API deadline enforcement

**IMPLEMENTED — pending CI verification**

Branch: feat/inference-deadline-propagation

- [x] Add optional deadline_ms (1–3,600,000 ms) to the OpenAI-compatible chat request.
- [x] Apply the request budget to FIFO admission wait without replacing the configured queue timeout.
- [x] Return HTTP 504 when the request deadline expires before generation or during non-streaming generation.
- [x] Emit an SSE timeout_error followed by [DONE] when a streaming request expires after the response starts.
- [x] Cancel the in-flight provider coroutine and release the active inference slot on deadline expiry.
- [x] Add low-cardinality deadline metrics by phase and regression tests for queue timeout classification, completion cancellation, streaming cancellation, and request validation.
- [x] Document the HTTP/SSE behavior and explicitly state that the deadline is API-process scoped.

### Remaining R1/R2 work

- [ ] Integrate the full versioned inference envelope with the API/provider and validate model identity, context, evidence, usage, and structured error semantics.
- [ ] Propagate deadline and cancellation through autonomous worker requests.
- [ ] Replace separate API admission and worker direct model calls with one durable cross-process scheduler.
- [ ] Add concurrency/restart/crash-recovery tests proving global single-generation behavior.

**Verification status:** implementation is committed to a separate branch. Do not change the status to VERIFIED until the CI run for the exact branch head completes successfully and its test results are reviewed.
