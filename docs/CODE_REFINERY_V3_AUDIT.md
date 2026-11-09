# Laporan Audit dan Perubahan — RUSA AI Advanced Code Refinery v3.0

**Tanggal audit:** 9 November 2026  
**Repository:** `StrategyG/RusaAi`  
**Branch:** `feat/aicode-code-refinery-v3`  
**Pull request:** [#88 — Advanced Code Refinery v3.0](https://github.com/StrategyG/RusaAi/pull/88)  
**Commit baseline yang divalidasi pada audit awal:** `6df683a762fe2a52e10d77bc838851799c7f7621`  
**CI historis:** [42569137903](https://github.com/StrategyG/RusaAi/actions/runs/42569137903) — seluruh job pada commit baseline tersebut lulus.  
**Status saat ini:** Draft PR; perubahan setelah baseline ini belum disertifikasi oleh run historis. CI wajib lulus kembali pada head PR terbaru. Belum di-merge atau di-deploy.

## 1. Ringkasan keputusan

Konfigurasi yang diterapkan:

| Area | Keputusan |
|---|---|
| Sandbox | Hybrid: Docker rootless untuk pemeriksaan standar; MicroVM untuk pytest berisiko tinggi |
| LLM | Primary OpenAI-compatible → local OpenAI-compatible → mock fail-closed |
| Autonomy | Approval: branch kerja dan draft PR diperbolehkan; merge, release, deploy, perubahan secret/permission tetap memerlukan persetujuan manusia |
| Quality gate | Skor minimum 85/100, ditambah gate sintaks dan keamanan wajib |

Perubahan memperluas runtime AICode yang sudah ada. Tidak dibuat orchestrator, antrean, atau workflow store kedua.

## 2. Perubahan implementasi

### 2.1 AST Code Quality Engine

- Analisis hanya file Python yang berubah, staged, atau untracked pada worktree Git.
- Batas inventaris: 2.000 file, 1 MB per file, dan 100 MB agregat.
- Source diparse menggunakan AST tanpa import atau eksekusi source.
- Dimensi skor: type safety 15%, documentation 10%, error handling 15%, code quality 15%, complexity 10%, security 20%, testability 15%.
- Ambang default 85; dapat diatur melalui `RUSA_REFINERY_MIN_SCORE`.
- Syntax error, input yang tidak dapat dianalisis, credential-like literal, dan primitive eksekusi berbahaya memblokir gate.
- Laporan kanonis memiliki SHA-256; orchestrator memverifikasi skor, digest, syntax status, dan jumlah temuan blocking.
- Tidak adanya perubahan Python dilaporkan sebagai `not_applicable`, bukan sebagai bukti kualitas source.

Skor merupakan heuristik yang deterministik. Skor bukan bukti kebenaran fungsional, coverage, atau tidak adanya kerentanan.

### 2.2 Hybrid LLM

- Provider utama memakai konfigurasi `RUSA_MODEL_BASE_URL` dan `RUSA_MODEL_NAME`.
- Fallback lokal memakai `RUSA_LOCAL_MODEL_BASE_URL`, `RUSA_LOCAL_MODEL_NAME`, dan kredensialnya sendiri.
- Kredensial provider utama tidak diwariskan ke provider lokal.
- Respons harus memenuhi JSON action envelope yang dibatasi.
- Mock hanya mengembalikan `blocked=true`; mock tidak menghasilkan kode atau memalsukan bukti verifikasi.
- Endpoint lokal wajib tersedia dan menggunakan HTTPS pada konfigurasi produksi.

### 2.3 Hybrid sandbox dan credential boundary

- Pemeriksaan biasa menggunakan Docker dengan network disabled, read-only filesystem, non-root user, dropped capabilities, batas PID, CPU, memori, dan snapshot.
- Pytest berisiko tinggi diarahkan ke MicroVM. Jika MicroVM tidak tersedia atau bukti tidak cocok, eksekusi diblokir; tidak ada fallback diam-diam ke Docker.
- Classifier mendeteksi dynamic import/execution, primitive proses/host, shell subprocess yang tidak dapat dipastikan aman, native/binary artifacts, install lifecycle hooks, dan source yang tidak dapat diklasifikasi.
- Snapshot MicroVM deterministik, mengecualikan Git metadata dan path/file sensitif, serta memeriksa pola credential berkeyakinan tinggi.
- Docker dan MicroVM menggunakan descriptor no-follow saat membaca file snapshot.
- Gateway memblokir credential berkeyakinan tinggi dari file read, Git diff, dan file write agar tidak masuk ke konteks agent atau workspace.
- Produksi mewajibkan HTTPS, `RUSA_MICROVM_RUNNER_IMAGE_DIGEST` yang dipatok, dan verifikasi digest pada readiness serta hasil eksekusi.

Credential scanner mengenali pola umum berkeyakinan tinggi. Ia bukan secret scanner/DLP yang komprehensif.

### 2.4 Audit dan observability

- Ringkasan evidence worker disimpan dalam audit event, termasuk metrik kualitas dan temuan terbatas.
- Prometheus metrics ditambahkan untuk request LLM, skor kualitas, hasil laporan, serta eksekusi dan durasi sandbox.
- Label metrics dibatasi untuk menghindari cardinality tinggi.

## 3. Hasil pengujian dan CI

Semua tiga job pada CI run `42569137903` berhasil:

| Job | Hasil |
|---|---:|
| Test, lint, and security | PASS |
| Build, test, and scan production images | PASS |
| RAG and MCP integration tests | PASS |

Rincian:

- Test suite utama: **238 passed, 6 skipped, 1 warning**.
- Full test suite di isolated sandbox image: **217 passed, 27 skipped, 1 warning**.
- RAG/MCP integration suite: **45 passed**.
- Compile, Ruff lint, Bandit static analysis, Docker image builds, dan production Compose validation: lulus.
- Bandit tetap mengeluarkan peringatan `nosec` B108 pada temporary paths dan B104 pada bind address yang sudah diberi anotasi. Peringatan ini tidak menggagalkan CI.

Enam skip pada test suite utama:
1. Optional document parser dependencies tidak terpasang.
2. PostgreSQL integration test tidak dijalankan karena `RUSA_TEST_DATABASE_URL` tidak dikonfigurasi.
3. Optional MCP runtime tidak terpasang (dua test modules).
4. PyTorch tidak terpasang untuk training tokenization.
5. Optional local-model dependencies tidak terpasang.

Skip bukan test pass; cakupan untuk dependency dan layanan yang tidak tersedia tetap belum dibuktikan oleh run ini.

## 4. Hasil security scan image

Trivy memindai empat image Debian 13.7:

| Image | HIGH | CRITICAL |
|---|---:|---:|
| API | 51 | 0 |
| Agent sandbox | 56 | 0 |
| Worker | 55 | 0 |
| RAG/MCP | 51 | 0 |

Angka tersebut adalah temuan package/vulnerability, bukan jumlah CVE unik. Beberapa temuan berasal dari paket OS seperti util-linux, expat, sqlite, dan ncurses. Gate CI menggunakan `--ignore-unfixed` dan berhasil, artinya Trivy tidak menemukan temuan HIGH/CRITICAL yang dianggap dapat diperbaiki pada saat scan tersebut.

**Catatan penting:** CI hijau tidak berarti image bebas kerentanan. Temuan HIGH yang belum memiliki versi perbaikan tetap merupakan residual risk dan harus ditinjau pemilik base image sebelum produksi.

## 5. Temuan residual dan batas audit

| Tingkat | Temuan | Dampak / tindak lanjut |
|---|---|---|
| HIGH / blocker produksi | Firecracker/MicroVM supervisor belum disediakan atau diuji secara end-to-end | PR hanya menyediakan client, kontrak, routing, dan MockTransport tests. Provision supervisor nyata, readiness, isolation, resource limits, network policy, dan runner image sebelum produksi. |
| HIGH | Trivy melaporkan 51–56 temuan HIGH per image, 0 CRITICAL | Tinjau paket OS/base image dan catat risk acceptance atau perbaikan saat paket upstream tersedia. |
| MEDIUM | Adapter MicroVM memakai HTTPS + bearer token; client certificate mTLS belum dikonfigurasi di PR ini | ADR komunikasi yang dilampirkan berstatus **Diusulkan** dan menetapkan mTLS untuk komunikasi internal. Gunakan service mesh yang menerapkan mTLS atau tambahkan dukungan sertifikat sebelum menyatakan kesesuaian penuh ADR. |
| MEDIUM | Digest runner diverifikasi terhadap nilai yang dipatok, tetapi respons API bukan hardware-backed attestation | Supervisor tetap harus menjadi layanan tepercaya, melindungi signing/provenance dan runner image, serta diaudit terpisah. |
| MEDIUM | Enam test utama dan 27 test sandbox di-skip karena dependency/layanan opsional tidak tersedia | Jalankan kembali pada environment dengan PostgreSQL, MCP runtime, parser, PyTorch, dan local-model dependencies bila fitur tersebut masuk acceptance scope. |
| LOW | Credential scanner hanya mencakup pola berkeyakinan tinggi | Pertahankan secret-scanning khusus di CI/repository; jangan menganggap scanner ini sebagai pengganti secret-management atau DLP. |

### Batas kesesuaian dengan dokumen arsitektur

- `ADR-RUSA-AI-Komunikasi-Agent` yang dilampirkan berstatus **Diusulkan**. PR ini tidak mengklaim telah mengimplementasikan seluruh topologi gRPC/Protobuf, RabbitMQ, Kafka, transactional outbox, message signing, atau mTLS.
- `RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE` juga berstatus **Proposed Architecture** dengan delapan tahap/agent DevOps. PR ini memperkuat jalur AICode enam specialist roles yang sudah ada; ini bukan implementasi penuh delapan agent lifecycle.
- Human approval untuk merge branch terlindungi, release/deploy produksi, secret/permission changes, dan aksi destruktif tetap menjadi batas kewenangan. Tidak ada merge atau deployment otomatis yang ditambahkan.

## 6. Rekomendasi tindak lanjut

1. Pertahankan PR sebagai Draft sampai review manusia selesai.
2. Provision MicroVM supervisor nyata, pin `RUSA_MICROVM_RUNNER_IMAGE_DIGEST`, dan jalankan integration/penetration tests pada supervisor tersebut.
3. Penuhi mTLS melalui service mesh atau implementasi client certificate untuk adapter.
4. Tinjau dan remediasi/terima secara formal temuan HIGH Trivy pada base image.
5. Jalankan test tambahan dengan dependency dan layanan opsional yang saat ini di-skip.
6. Setelah seluruh approval diberikan, maintainer dapat memutuskan merge secara manual. Tidak ada deployment yang dilakukan oleh pekerjaan ini.


## 7. Addendum — system-wide hardening after the baseline audit

The earlier CI run above is historical evidence for its exact commit only. It does not certify subsequent changes. The integration branch has since received additional cross-cutting hardening:

- Strict deployment-environment normalization and fail-closed validation.
- Explicit-empty policy allowlist preservation and invalid risk/action rejection.
- Boundary-aware credential scanning across agent prompts, complete model action envelopes, tool output, and legacy RAG retrieval.
- Alias-aware AST security analysis for aliased imports, wildcard imports, dynamic imports, and dangerous process primitives.
- Cooperative cancellation with `cancel_requested`, `cancel_epoch`, a 30-second lease and 10-second heartbeat defaults.
- Human reconciliation for ambiguous external side effects. Unresolved cancellation cannot be retried or cancelled again; `tenant_admin` and `platform_admin` must record `NONE` or `APPLIED` with bounded evidence through `POST /v1/aicode/tasks/{task_id}/reconcile`.
- New migrations 010 and 011, queue/worker/API integration tests, same-origin web proxy route, and Control Center reconciliation UI.
- Validation errors no longer echo raw request inputs; missing mandatory verification evidence remains `awaiting_approval`, not success or an automatic retry.

### Current validation rule

A current green result must match the latest PR head SHA and include the backend test/lint/security workflow, RAG/MCP integration workflow, production image scan, and web type-check/build/container workflow. Pending, cancelled, skipped, or older runs do not count. The optional-service skips recorded in the historical baseline remain residual validation gaps until those services are available and the tests execute.

The architecture boundary remains unchanged: this branch does not implement the proposed RabbitMQ/Kafka topology, transactional outbox, detached JWS, internal mTLS, all ten core agents, or all eight DevOps lifecycle agents. See `docs/SYSTEM_ALIGNMENT_AUDIT.md` for the complete implementation matrix.
