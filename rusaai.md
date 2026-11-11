---
title: "RusaAi — Master Roadmap"
project: "RusaAi"
document: "rusaai.md"
version: "1.0.0"
updated: "2026-11-12"
status: "Working roadmap — implementation status must be revalidated before release"
language: "id"
---

# RusaAi — Master Roadmap

> Roadmap induk pengembangan RusaAi. Dokumen ini menyatukan target arsitektur, strategi model/training, deployment CPU, agent system, dan kondisi repository. Status setiap pekerjaan harus dibuktikan melalui commit, test, artifact, benchmark, atau acceptance report — bukan hanya konfigurasi atau dokumentasi.

## 1. Tujuan dan ruang lingkup

Roadmap ini menjadi urutan kerja bersama untuk membangun RusaAi sebagai ekosistem AI lokal CPU-first yang terdiri dari:

1. **Rusaai — Control Plane:** pengguna, agent, workflow, task state, context, memory, policy, tool execution, audit, dan verifikasi.
2. **RusaModels — Model Plane:** model registry, artifact lifecycle, model loading/residency, CPU inference runtime, scheduler, dan telemetry model.
3. **RusaAi-Edge — Neural Core:** target model bahasa lokal; spesifikasi 1.541B adalah target arsitektur, bukan bukti bahwa checkpoint atau bobot sudah tersedia.
4. **Carbon Profile:** pemahaman input dan preprocessing; kemampuan OCR/ASR penuh tidak diasumsikan tersedia pada MVP.
5. **Astra Profile:** reasoning, planning, tool use, dan penyelesaian tugas; reasoning controller perlu dibangun dan dievaluasi.
6. **Shared Contracts:** kontrak API dan schema berversi yang menghubungkan Control Plane dan Model Plane.

Roadmap ini mencakup konsolidasi repository, pemilihan model, data dan training, inference, agent orchestration, memory/RAG, keamanan, deployment Windows/Android, evaluasi, dan release.

## 2. Sumber acuan dan aturan konflik

Dokumen berikut menjadi sumber proyek yang digunakan saat menyusun roadmap:

| Sumber | Peran dalam roadmap | Status sumber |
|---|---|---|
| `RusaAi_Architecture_v2.0.md` | Target arsitektur, agent, inference, memory, security, deployment, acceptance | Proposed Architecture — Not Yet Implemented |
| `RusaAi_Training_Engine_v1.0.md` | Strategi checkpoint non-Qwen, dataset, training tracks, evaluasi, manifest | Proposed resource-constrained pilot |
| `RUSA_AI_CPU_Deployment_Configuration_v1.0.md` | Baseline CPU, resource policy, deployment dan acceptance awal | Baseline desain v1.0 |
| `RUSA_AI_All_Agents-2.md` | Identitas, kewenangan, workflow, dan communication contract agent | Spesifikasi agent |
| `MASTER_CONTEXT.md` | Tata kelola konteks, kesinambungan, dan standar kerja proyek | Pedoman kerja |

### 2.1 Konflik model yang wajib tetap terlihat

`RUSA_AI_CPU_Deployment_Configuration_v1.0.md` mencantumkan Qwen3-4B Instruct sebagai model family untuk baseline `rusa-cpu-16`. Sementara itu, Architecture v2.0 menetapkan RusaAi-Edge-1.54B sebagai target neural core dan Training Engine v1.0 menetapkan strategi foundation non-Qwen.

Karena ketiganya tidak identik, roadmap ini **tidak menganggap Qwen3-4B sebagai RusaAi-Edge** dan tidak mengubah konfigurasi lama secara diam-diam. Sebelum model dipromosikan, buat ADR yang menetapkan apakah Qwen3-4B hanya legacy/compatibility baseline atau tetap dipakai untuk jalur terpisah. Jalur produk v2.0 harus melewati audit non-Qwen dan artifact gate.

### 2.2 Identitas model

- `rusaai-edge-1.54b`: hanya untuk checkpoint yang benar-benar cocok dengan spesifikasi referensi v2.0 dan lolos audit.
- `rusaai-edge-derived`: untuk checkpoint non-Qwen yang disetujui tetapi arsitekturnya berbeda dari target referensi.
- `rusaai-research-100m`: model riset dari random initialization; bukan model produksi.
- `rusaai-cpu-smoke-test`: alias transport/test saja; bukan identitas model agent atau release.

Kandidat `BhinekaIntiLabs/bhineka-gpt` sekitar 556,3M parameter adalah kandidat pilot untuk diaudit, bukan dependency yang sudah disetujui. Model card yang dirujuk Training Engine menyebut context 2K; gunakan 2K sampai context extension diuji secara independen. Audit provenance, source code/custom model code, lisensi, tokenizer, arsitektur, dan jalur GGUF sebelum penggunaan.

## 3. Snapshot repository saat roadmap dibuat

Snapshot GitHub yang diverifikasi pada **2026-11-11 17:44 UTC**:

| Item | Kondisi |
|---|---|
| Repository | `StrategyG/RusaAi` |
| Default branch | `main` |
| `main` commit | `31a2bb74aaccaa6d3f5c8b1f0379df6c5d4bda7d` |
| Architecture branch | `feat/architecture-v2-cpu-first-alignment` |
| Branch head | `98c096a823371b0aa2ef63fc8faa39338018a114` |
| Pull request | [#107 — Align RusaAi repository with v2.0 CPU-first architecture](https://github.com/StrategyG/RusaAi/pull/107) |
| PR status | Open; mergeable; belum digabung ke `main` |
| Perubahan PR | 11 commits, 39 files changed |
| GitHub Actions run | [Run #4220](https://github.com/StrategyG/RusaAi/actions/runs/43069037219) |
| CI | Ketiga job berhasil: test/lint/security, RAG/MCP integration, production image build/test/scan |

Deskripsi PR melaporkan 275 passed dan 6 skipped untuk test/lint/security. Jumlah tersebut adalah laporan pada PR; verifikasi ulang diperlukan jika head commit berubah.

Fondasi repository yang telah tersedia mencakup FastAPI inference gateway, AICode/worker runtime, PostgreSQL task/lease controls, A2A task validation, RAG/MCP integration, sandbox/tooling, dan security/test foundations. PR #107 menambahkan alignment blueprint model, inference admission control, CPU profile, inference schemas, ADR, dokumentasi batas Control Plane/Model Plane, dan regression tests.

**Batas status:** CI lulus tidak berarti model inti telah dilatih, seluruh agent telah diimplementasikan, perangkat sudah diuji, atau sistem telah tersertifikasi production. `main` tetap menjadi baseline terpisah sampai PR ditinjau dan digabung secara eksplisit.

## 4. Prinsip pelaksanaan

1. **Evidence before status:** pekerjaan hanya berstatus selesai jika deliverable dan acceptance evidence tersedia.
2. **One shared model runtime:** sepuluh core agents dan delapan DevOps specialist adalah logical profiles; bukan delapan belas model yang berjalan bersamaan.
3. **CPU-first:** GPU offload `0`; model runtime memakai CPU-compatible llama.cpp/GGUF sesuai hasil audit.
4. **Bounded execution:** satu active generation pada baseline; pending queue dibatasi per device profile.
5. **Resource safety:** pertahankan sekurangnya 2 GB free RAM saat inference; angka konfigurasi adalah estimasi sampai diukur pada perangkat aktual.
6. **No false model identity:** nama alias tidak membuktikan asal-usul, arsitektur, atau kualitas bobot.
7. **Training is separate from inference:** training job tidak memakai interactive inference slot.
8. **Security boundary:** agent tidak mendapat shell/model access langsung; tool hanya melalui Tool Gateway, allowlist, sandbox, policy, audit, dan human approval untuk tindakan konsekuensial.
9. **Versioned contracts:** perubahan schema/API/model manifest harus kompatibel atau memakai versi baru dan ADR.
10. **No unsupported performance promise:** target tokens/second ditetapkan setelah CPU SKU dan benchmark perangkat diketahui.

## 5. Roadmap utama — fase dan gerbang

Fase disusun berdasarkan dependensi. Tidak ada estimasi tanggal kalender karena kapasitas perangkat, checkpoint, dan alokasi implementasi belum menjadi baseline terukur.

| Fase | Prioritas | Pekerjaan utama | Deliverable | Gerbang keluar |
|---|---|---|---|---|
| 0. Repository & architecture consolidation | P0 | Audit `main` dan PR terbuka yang relevan; tetapkan sumber acuan; catat konflik Qwen/non-Qwen; inventaris hardware; review PR #107 | Repository baseline, dependency map, ADR keputusan model/runtime | CI hijau pada head yang ditinjau; konflik sumber dicatat; tidak ada klaim fitur yang belum ada |
| 1. Foundation checkpoint audit | P0 | Pilih kandidat non-Qwen; audit provenance, lisensi, lineage, source/custom code, parameter, tokenizer, chat template, context, dan GGUF support | `ADR-TRAIN-001`, checkpoint decision record | Kandidat diterima/ditolak dengan bukti; immutable revision dan lisensi tercatat |
| 2. Data, tokenizer & evaluation foundation | P0 | Bangun ingestion, quality, dedup, PII/secret scrub, provenance, dataset split, tokenizer verification, frozen evaluation | Dataset builder, tokenizer report, Eval v1, immutable manifests | Dataset schema/provenance valid; leakage checks lulus; evaluation set dibekukan |
| 3. CPU model artifact & inference runtime | P0 | Siapkan model artifact yang disetujui; konversi/ambil GGUF yang sah; Q4_K_M; llama.cpp; checksum; chat template; runtime memory test | Model card, artifact manifest, GGUF, CPU benchmark awal | SHA-256 cocok; model load; GPU layers 0; prompt 128/1K/4K diuji; RAM dan latency dicatat |
| 4. Model Access Gateway & shared contracts | P0 | Validasi request, auth service-to-service, model ID/version, context budget, queue submit, deadline/cancellation, usage/performance, structured errors, output schema | Inference API v1 dan contract tests | Gateway benar-benar memakai schema; timeout/cancel end-to-end; unknown model ditolak; contract tests lulus |
| 5. Context, memory & RAG MVP | P1 | Context Manager; SQLite; FTS5; lima kategori memory; provenance; retrieval; citation/evidence | Local knowledge MVP | Retrieval mencatat source ID; ACL berlaku; citation correctness diuji; memory survive restart |
| 6. Shared scheduler & workflow recovery | P1 | Satu antrean lintas API/worker; priority policy; bounded queue; durable state; deadline; cancellation; non-blocking child-task yield; retry/backoff | Shared scheduler dan workflow checkpoint | Satu active generation; queue full/backpressure benar; FIFO/prioritas sesuai policy; recovery dan cancellation lulus |
| 7. Agent MVP | P1 | Integrasikan Supreme Orchestrator, Planner, Software Engineer/Code, dan QA; task DAG, unique task ID, acceptance criteria, evidence | Verified Agent MVP | End-to-end task selesai dengan evidence; task state pulih setelah restart; retry terbatas |
| 8. Verification, policy & tool security | P1 | ORM, PRM-Lite, independent QA, Tool Gateway, allowlist, sandbox, approval, audit ledger, prompt-injection tests | Verified execution MVP | Tool tidak bisa bypass gateway; destructive action tanpa approval = 0; schema/test/process checks lulus |
| 9. Full core agents & DevOps profiles | P2 | Lengkapi 10 core agents dan 8 DevOps specialist profiles sebagai logical roles di shared runtime | Full agent workflow | Semua role punya prompt, input/output contract, permission, owner, acceptance criteria, dan evaluasi |
| 10. Cross-device deployment | P2 | Windows 11 x64 CPU-only; Android ARM64 CPU-only; install/runtime, storage, thermal/resource, restart/recovery | Device deployment reports | Acceptance test per perangkat lulus; Android hanya inference, bukan training |
| 11. Carbon & Astra refinement | P2 | Carbon preprocessing dan multimodal adapters sesuai scope; Astra reasoning controller, routing, tool planning, reasoning profiles | Capability profiles | Setiap kemampuan memiliki test set dan batasan; OCR/ASR atau reasoning tidak diklaim sebelum tervalidasi |
| 12. Release qualification | P0 untuk release | Full regression, benchmark, security, safety, soak, artifact verification, model card, release manifest, rollback | RusaAi release candidate dan release report | Seluruh critical gates lulus; limitation dan known issues dipublikasikan; approval release tercatat |

### 5.1 Scope Agent MVP

MVP pertama tidak menunggu seluruh agent selesai. Gunakan empat role inti:

- **Supreme Orchestrator:** menerima tujuan, mengoordinasikan plan/delegasi, memantau, meminta QA, dan melaporkan status.
- **Planner:** menyusun milestone, dependency, prioritas, dan acceptance criteria.
- **Software Engineer / Code Agent:** mengimplementasikan perubahan melalui workspace dan tool yang diizinkan.
- **QA & Testing:** memeriksa hasil secara independen dan melampirkan evidence.

Task Decomposer, System Architect, DevOps, Security & Policy, Memory & Knowledge, serta Evaluation & Reporting diintegrasikan bertahap. Policy Engine dan Tool Gateway tetap menjadi kontrol wajib walaupun seluruh specialist belum lengkap.

### 5.2 Sepuluh core agents target

1. Supreme Orchestrator
2. System Architect
3. Planner
4. Task Decomposer
5. Software Engineer
6. DevOps
7. QA & Testing
8. Security & Policy
9. Memory & Knowledge
10. Evaluation & Reporting

### 5.3 Delapan DevOps specialist profiles

1. Plan Agent
2. Code Agent
3. Build Agent
4. Test Agent
5. Release Agent
6. Deploy Agent
7. Operate Agent
8. Monitor Agent

Spesialisasi tersebut adalah task profile/sub-agent logical di bawah DevOps Agent, bukan kebutuhan menjalankan delapan model terpisah.

## 6. Workstream Training Engine

Training Engine berjalan paralel tetapi tidak boleh mengganggu inference interaktif.

### Track A — Product adaptation (non-Qwen)

1. Audit dan pilih foundation checkpoint non-Qwen.
2. Jalankan baseline inference dan evaluation sebelum adaptasi.
3. Mulai dari system instructions, Carbon/Astra profiles, RAG, structured output, tool contracts, dan prompt regression.
4. Siapkan dataset dan adapter feasibility hanya setelah format checkpoint dan modul PEFT diverifikasi.
5. Pada perangkat 8 GB, LoRA hanya pilot untuk kandidat sekitar 0,5B atau lebih kecil, dan hanya jika one-step forward/backward, memory budget, save/resume, dan no-OOM gate lulus.
6. LoRA 1B–1.5B tidak termasuk jaminan 8 GB; tunda sampai tersedia host 32–64 GB untuk feasibility test.

### Track B — RusaAi Research 100M from scratch

- Target riset sekitar 100M parameter; arsitektur referensi: 12 layers, hidden 768, 12 query heads, 4 KV heads, SwiGLU intermediate 2.048, vocabulary 32K, context design 4K.
- Mulai dari tokenizer dan smoke test 1M token; lanjut 5M token hanya bila resource stabil.
- Catat train/validation loss, checkpoint save/resume, RAM, CPU, dan seed.
- Hasilnya adalah validasi pipeline riset, bukan foundation model produksi atau pengganti RusaAi-Edge-1.54B.

### Track C — Runtime adaptation tanpa weight training

System instruction, RAG, project memory, SQLite/FTS5, structured output, Tool Gateway, ORM, PRM-Lite, dan prompt regression menjadi jalur peningkatan produk yang realistis pada perangkat terbatas.

### Urutan implementasi Training Engine

| Urutan | Pekerjaan | Deliverable / exit gate |
|---|---|---|
| T0 | Device/software inventory | Hardware report dengan CPU SKU, RAM fisik, storage, OS |
| T1 | Foundation candidate audit | ADR-TRAIN-001: provenance, license, architecture, tokenizer |
| T2 | Dataset pipeline | Schema, provenance, PII, dedup, split/leakage tests |
| T3 | Frozen evaluation suite | 800–1.200 prompts berversi; target awal 1.000 |
| T4 | CPU memory smoke test | Process/system memory dalam budget |
| T5 | 100M scratch pilot | Loss finite; checkpoint resume bekerja |
| T6 | Small-base LoRA feasibility | One-step forward/backward dan memory gate lulus |
| T7 | Product adaptation | Prompt/RAG atau adapter dengan improvement terukur |
| T8 | Export & quantization | GGUF, template/tokenizer, Q4_K_M, checksum, inference |
| T9 | RusaModels integration | Model registry entry dan inference contract lulus |
| T10 | Rusaai Agent MVP integration | Orchestrator, Planner, Engineer, QA berjalan |
| T11 | Full evaluation | Release report seluruh domain dan safety gates |
| T12 | Release | Model card, manifest, limitation, release approval |

### 6.1 Dataset dan evaluation baseline

Target awal SFT pilot: **10.000–20.000 contoh** atau sekitar **2–8 juta token**. Mixture usulan: Indonesian 30%, English/bilingual 10%, software engineering 15%, architecture/DevOps 15%, reasoning 10%, agent/tool 10%, safety/policy 10%. Persentase ini adalah target awal yang harus diuji, bukan hasil dataset yang sudah tersedia.

Frozen evaluation: **800–1.200 prompts**; komposisi awal 1.000 prompt meliputi Indonesian 200, English/bilingual 100, reasoning 120, coding 150, architecture/DevOps 120, tool/JSON 120, RAG 90, safety/prompt injection 100. Evaluation data tidak boleh bocor ke training atau synthetic-data loop.

## 7. Deployment profile dan resource policy

| Parameter | `rusa-cpu-16` | `rusa-edge-8` |
|---|---|---|
| Device | Windows 11 x64, RAM fisik 16 GB | Laptop/Android ARM64, RAM fisik 8 GB |
| Model target | RusaAi-Edge-1.54B setelah checkpoint cocok dan lolos audit | Checkpoint quantized yang lolos audit; jangan menganggap semua 1B–1.5B pasti muat |
| Quantization | Q4_K_M | Q4_K_M |
| Context | Mulai 4K; 8K hanya setelah RAM/context test | 4K sebagai target profile; gunakan context checkpoint aktual jika lebih rendah |
| Active generation | 1 | 1 |
| Pending queue | Maksimal 3 | Kecil dan bounded; mulai 1 |
| GPU offload | 0 | 0 |
| RAM reserve | Minimal 2 GB free | Minimal 2 GB free; hentikan/kurangi beban bila tidak tercapai |
| Runtime | llama.cpp CPU-compatible | llama.cpp CPU-compatible |
| Thread policy | Deteksi CPU SKU/core dan benchmark | Deteksi perangkat; jangan menyalin angka thread PC ke Android |

Angka thread, batch, KV cache, dan memory limit adalah titik awal konfigurasi, bukan jaminan performa. Untuk kandidat Bhineka-GPT yang model card-nya menyebut context 2K, jangan mengiklankan atau mengaktifkan 4K tanpa validasi context extension. RAM virtual Android tidak dihitung sebagai pengganti RAM fisik.

## 8. Acceptance criteria lintas sistem

### 8.1 Model dan artifact

- [ ] Foundation non-Qwen provenance dan lisensi dikonfirmasi.
- [ ] Checkpoint revision immutable, tokenizer, chat template, architecture, parameter count, dan custom code diaudit.
- [ ] Model ID sesuai identitas artifact; `rusaai-edge-1.54b` hanya untuk arsitektur referensi yang cocok.
- [ ] GGUF diperoleh/dibangun secara sah; quantization dicatat; SHA-256 diverifikasi.
- [ ] Model load berhasil dengan GPU offload `0`.
- [ ] Context 4K diuji; 8K hanya setelah memory gate.
- [ ] TTFT, tokens/second, total latency diukur pada prompt 128, 1K, dan 4K.

### 8.2 Runtime dan resource

- [ ] Peak RAM dan minimum free RAM tercatat.
- [ ] Satu active generation terjaga.
- [ ] Queue bounded, deadline, timeout, cancellation, dan backpressure bekerja.
- [ ] Model tidak reload pada setiap agent turn.
- [ ] Sesi CPU 30 menit stabil tanpa crash/OOM atau swap berlebihan.
- [ ] Thermal throttling dicatat jika telemetry tersedia.

### 8.3 Orchestration

- [ ] Setiap task memiliki `task_id`, parent/dependency, owner, priority, deadline, dan acceptance criteria.
- [ ] Parent task yield saat menunggu child task.
- [ ] Retry terbatas dan hanya untuk error yang dapat dipulihkan.
- [ ] Task state dan queue pulih setelah restart.
- [ ] Completion wajib memiliki evidence dan unresolved issues.
- [ ] Tidak ada agent yang melewati Policy Engine.

### 8.4 Memory, RAG, dan verification

- [ ] Working, Episodic, Semantic, Procedural, dan Project Memory berjalan.
- [ ] Retrieval menyimpan source ID, provenance, dan metadata.
- [ ] Informasi yang belum diverifikasi diberi label.
- [ ] RAG citation correctness dan ACL diuji.
- [ ] ORM memeriksa schema, test, dan outcome.
- [ ] PRM-Lite memeriksa process rules.
- [ ] QA independen dari agent pembuat output.

### 8.5 Security dan tool execution

- [ ] Tool allowlist dan permission policy diterapkan.
- [ ] Model tidak memiliki shell access langsung.
- [ ] Sandbox membatasi filesystem dan network.
- [ ] Secret tidak masuk ke prompt, log, atau memory biasa.
- [ ] Aksi destruktif/konsekuensial membutuhkan human approval.
- [ ] Prompt injection, data exfiltration, dan secret leakage tests dijalankan.
- [ ] Audit ledger merekam tindakan dan hasil.
- [ ] Cloud fallback nonaktif secara default kecuali diputuskan secara eksplisit.

### 8.6 Device

- [ ] Windows 11 x64 CPU-only berjalan dan diuji.
- [ ] Android ARM64 CPU-only berjalan dan diuji.
- [ ] GPU offload dinonaktifkan.
- [ ] Restart, recovery, storage, resource, dan thermal behavior diuji.
- [ ] Training tidak dijalankan pada Android.

### 8.7 Evaluation/release gates

Threshold berikut adalah **target awal yang diusulkan oleh Training Engine**, bukan hasil pengukuran saat ini:

| Metrik | Target awal |
|---|---:|
| JSON schema validity | ≥98% |
| Required tool permission compliance | 100% pada test suite |
| Unauthorized destructive tool execution | 0 |
| Citation/source attribution correctness | ≥95% pada grounded RAG set |
| Critical safety tests | 100% pass |
| Indonesian human evaluation | Mean ≥4/5 pada rubric terkurasi |
| Runtime stability | 30 menit tanpa OOM |
| Artifact integrity | 100% checksum verification |

Setiap release report harus mencantumkan baseline comparison, regression per domain, failure examples, limitations, dan confidence interval bila relevan. Nilai agregat tinggi tidak dapat menutupi kegagalan safety atau permission yang kritis.

## 9. Risiko utama dan mitigasi

| Risiko | Dampak | Mitigasi / stop condition |
|---|---|---|
| Foundation checkpoint non-Qwen tidak lolos audit | Model target tidak dapat dipromosikan | Tolak kandidat; jangan mengganti identitas secara diam-diam; evaluasi kandidat lain |
| Candidate context hanya 2K | Context target 4K/8K tidak valid | Pertahankan context terverifikasi; context extension harus punya test dan report |
| 1B–1.5B training pada 8 GB | OOM, swap, hasil tidak valid | Jangan jalankan full fine-tuning; gunakan prompt/RAG; LoRA hanya setelah feasibility gate |
| Model alias tidak sesuai artifact | Salah identitas dan klaim kemampuan | Model manifest, revision, tokenizer, quantization, SHA-256, runtime validation |
| API dan worker punya antrean terpisah | FIFO global dan deadline tidak terjamin | Implementasikan satu shared scheduler lintas proses |
| RAM reserve tidak tercapai | Crash, swap, device tidak responsif | Resource monitor, backpressure, workload shedding, stop threshold |
| Agent melakukan aksi tanpa otorisasi | Kerusakan data/repository atau kebocoran | Tool Gateway, allowlist, sandbox, approval, audit |
| Dataset leakage/PII/license issue | Evaluasi tidak valid dan risiko hukum | Provenance, scrub, source-level split, dedup, license review |
| CI hijau dianggap production certification | Klaim kesiapan berlebihan | Pisahkan CI, artifact, hardware benchmark, safety, soak, dan release gates |

## 10. Tata kelola repository dan perubahan arsitektur

1. Kerjakan perubahan melalui branch/PR; jangan menulis langsung ke `main` tanpa review dan approval.
2. Satu PR harus memiliki scope, tests, evidence, rollback note, dan daftar file yang berubah.
3. Perubahan model identity, architecture dimensions, training method, API contract, security boundary, atau deployment resource policy wajib memiliki ADR.
4. Setiap artifact release harus immutable dan mempunyai checksum.
5. Jangan menggabungkan PR yang saling bertentangan sebelum dependency dan base branch diperiksa.
6. Tutup atau merge PR hanya setelah test, security, image build/scan, compose validation, dan review yang relevan lulus.
7. Perbarui bagian snapshot repository setiap kali branch/PR/CI berubah; snapshot lama tidak boleh diperlakukan sebagai status terkini.
8. Status `implemented` membutuhkan code path aktif dan test; status `verified` membutuhkan hasil uji yang dapat direproduksi; status `released` membutuhkan manifest dan approval.

### 10.1 Status label standar

- `NOT_STARTED` — belum ada implementasi.
- `DESIGN` — baru ada rancangan/dokumentasi.
- `IN_PROGRESS` — implementasi sedang berjalan.
- `IMPLEMENTED` — kode tersedia dan test relevan ada.
- `VERIFIED` — acceptance test dan evidence lulus.
- `BLOCKED` — ada dependency/risiko yang mencegah pekerjaan.
- `DEFERRED` — sengaja ditunda dengan alasan tercatat.
- `RELEASED` — artifact/release resmi diterbitkan.

## 11. Prioritas eksekusi berikutnya

Urutan kerja yang direkomendasikan dari kondisi repository saat snapshot:

1. Review PR #107 secara menyeluruh dan pastikan seluruh perubahan arsitektur benar-benar konsisten dengan source docs; CI Run #4220 telah lulus, tetapi merge tetap memerlukan review/approval.
2. Buat ADR keputusan model: pisahkan konfigurasi Qwen3-4B v1.0, target RusaAi-Edge-1.54B v2.0, dan kandidat non-Qwen Training Engine.
3. Lengkapi device inventory: CPU SKU, RAM fisik, storage, Windows build, Android device/build, dan resource baseline.
4. Jalankan checkpoint/license/provenance audit untuk kandidat non-Qwen; jangan tetapkan model produksi sebelum audit.
5. Bangun artifact manifest dan CPU inference smoke/benchmark untuk kandidat yang lolos.
6. Integrasikan inference schema dengan endpoint dan shared scheduler; saat ini schema additive tidak otomatis berarti endpoint sudah menggunakannya.
7. Selesaikan memory/RAG, agent MVP, ORM/PRM-Lite, dan Tool Gateway end-to-end.
8. Baru perluas seluruh agent/specialist, Carbon/Astra, device support, dan release qualification.

## 12. Definition of Done RusaAi

RusaAi dapat dinyatakan mencapai release target hanya jika seluruh kondisi berikut terbukti:

- Neural core memiliki checkpoint non-Qwen yang disetujui, model card, tokenizer, manifest, dan checksum.
- CPU inference bekerja pada profile yang ditargetkan dengan benchmark perangkat aktual.
- Rusaai dan RusaModels berkomunikasi melalui kontrak berversi yang benar-benar diterapkan.
- Shared scheduler mengendalikan satu active generation, bounded queue, deadline, cancellation, dan recovery.
- Agent workflow mempunyai task state durable, policy, tool authorization, evidence, dan independent QA.
- Lima kategori memory dan RAG mempunyai provenance serta retrieval evaluation.
- ORM, PRM-Lite, safety, dan security gates lulus.
- Windows 11 dan Android ARM64 masing-masing mempunyai acceptance report.
- Seluruh release gates, artifact integrity, rollback, model card, limitations, dan approval tersedia.

**Sebelum seluruh kondisi ini terpenuhi, status proyek tetap development/integration — bukan production-certified.**

## 13. Referensi proyek

- `RusaAi_Architecture_v2.0.md` — Unified CPU-First AI Architecture v2.0.
- `RusaAi_Training_Engine_v1.0.md` — Hybrid Training Blueprint, non-Qwen, CPU-only, 8 GB RAM.
- `RUSA_AI_CPU_Deployment_Configuration_v1.0.md` — CPU deployment baseline v1.0.
- `RUSA_AI_All_Agents-2.md` — Complete Agent Documentation dan Agent Communication Contract.
- `MASTER_CONTEXT.md` — Pedoman kerja Project dan kesinambungan konteks.
- Repository: https://github.com/StrategyG/RusaAi
- PR arsitektur: https://github.com/StrategyG/RusaAi/pull/107
- CI snapshot: https://github.com/StrategyG/RusaAi/actions/runs/43069037219

---

## Change log

| Versi | Tanggal | Perubahan |
|---|---|---|
| 1.0.0 | 2026-11-12 | Roadmap induk dibuat dari sumber arsitektur v2.0, Training Engine v1.0, CPU Deployment v1.0, All Agents, MASTER_CONTEXT, dan snapshot repository/PR #107. |