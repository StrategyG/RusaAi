"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Activity, AlertTriangle, Archive, ArrowDownRight, ArrowRight, Bot, BrainCircuit,
  CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, CircleDot, Clock3, Code2,
  Command, Database, FileCheck2, FileJson, FileText, Gauge, GitBranch, GitPullRequest,
  HardDrive, KeyRound, LayoutDashboard, Layers3, ListChecks, LockKeyhole, LogOut,
  Menu, MessageSquare, Network, Package, Play, Plus, RefreshCw, Rocket, Search,
  Send, Server, ShieldAlert, ShieldCheck, Sparkles, Terminal, Workflow, X, Zap
} from "lucide-react";

type Identity = { subject: string; tenant_id: string; roles: string[] };
type TaskStatus = "queued" | "running" | "awaiting_approval" | "succeeded" | "failed" | "cancelled" | string;
type Task = {
  task_id: string;
  tenant_id: string;
  status: TaskStatus;
  attempts: number;
  max_attempts: number;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
  payload?: {
    objective?: string;
    context?: string;
    repository?: { full_name?: string; base_branch?: string; work_branch_prefix?: string };
    acceptance_criteria?: string[];
  };
  result?: Record<string, unknown> | null;
};
type TaskPage = { items: Task[]; total: number; limit: number; offset: number };
type Health = {
  liveness: { ok: boolean; status: number; data?: Record<string, unknown> | null };
  readiness: { ok: boolean; status: number; data?: Record<string, unknown> | null };
};
type ChatLine = { role: "user" | "assistant"; content: string };
type PageId =
  | "overview" | "orchestrator" | "tasks" | "approvals" | "agents" | "devops"
  | "chat" | "knowledge" | "repository" | "evaluation" | "governance"
  | "observability" | "pillars" | "developer";
type TaskFilter = "all" | "active" | "queued" | "running" | "awaiting_approval" | "succeeded" | "failed" | "cancelled";

const PAGE_META: Record<PageId, { title: string; eyebrow: string; description: string }> = {
  overview: { title: "Command Center", eyebrow: "CONTROL PLANE", description: "Ringkasan tenant, task queue, dan kesiapan layanan berdasarkan data yang benar-benar tersedia." },
  orchestrator: { title: "Orchestrator", eyebrow: "WORKFLOW CONTROL", description: "Pisahkan workflow Supreme Orchestrator pada dokumen arsitektur dari pipeline AICode yang benar-benar ada di source code." },
  tasks: { title: "AICode Tasks", eyebrow: "EXECUTION WORKSPACE", description: "Kelola task coding tenant-scoped melalui endpoint AICode yang tersedia." },
  approvals: { title: "Approval Center", eyebrow: "HUMAN GOVERNANCE", description: "Pantau task yang menunggu review manusia tanpa mengarang aksi approve atau reject yang belum tersedia." },
  agents: { title: "Agent Workspace", eyebrow: "AGENT INTELLIGENCE", description: "Lihat 7 komponen runtime yang terdefinisi di kode dan 10 peran agent dalam dokumen arsitektur." },
  devops: { title: "Agentic DevOps", eyebrow: "LIFECYCLE WORKSPACE", description: "Delapan tahap lifecycle sesuai dokumen DevOps, dengan status implementasi yang dibedakan secara eksplisit." },
  chat: { title: "RUSA AI Chat", eyebrow: "MODEL INTERFACE", description: "Inference melalui RUSA API dengan model discovery dan streaming bila provider mendukungnya." },
  knowledge: { title: "Memory & Knowledge", eyebrow: "KNOWLEDGE PLANE", description: "Taksonomi memori sesuai spesifikasi agent; operasi CRUD dan retrieval belum diekspos oleh API saat ini." },
  repository: { title: "Repository & Artifacts", eyebrow: "EVIDENCE LAYER", description: "Tampilkan referensi artefak dan evidence yang dikembalikan task, bukan file browser yang belum tersedia." },
  evaluation: { title: "Evaluation & Reporting", eyebrow: "QUALITY INTELLIGENCE", description: "Definisi KPI dari dokumen agent dipisahkan dari metrik aktual yang belum tersedia sebagai API evaluasi." },
  governance: { title: "Governance & Policy", eyebrow: "SECURITY CONTROL", description: "Identitas tenant, role, permission task, dan batas tindakan yang didefinisikan di backend." },
  observability: { title: "Observability", eyebrow: "PLATFORM HEALTH", description: "Liveness, readiness, dan metrics operator. Progres task saat ini menggunakan polling, bukan WebSocket." },
  pillars: { title: "RUSA AI Product Pillars", eyebrow: "PRODUCT ARCHITECTURE", description: "Empat pilar utama RUSA AI dan batas integrasi yang benar-benar terlihat di repository." },
  developer: { title: "Developer Tools", eyebrow: "API WORKBENCH", description: "Periksa model yang tersedia dan validasi A2A task envelope melalui endpoint resmi." }
};

const NAV_GROUPS = [
  { label: "CONTROL", items: [{ id: "overview" as const, label: "Command Center", icon: LayoutDashboard }, { id: "orchestrator" as const, label: "Orchestrator", icon: Network }] },
  { label: "WORKSPACE", items: [{ id: "tasks" as const, label: "AICode Tasks", icon: Terminal }, { id: "approvals" as const, label: "Approvals", icon: ShieldCheck }, { id: "agents" as const, label: "Agent Workspace", icon: Bot }, { id: "devops" as const, label: "Agentic DevOps", icon: Workflow }] },
  { label: "INTELLIGENCE", items: [{ id: "chat" as const, label: "AI Chat", icon: MessageSquare }, { id: "knowledge" as const, label: "Memory & Knowledge", icon: BrainCircuit }, { id: "repository" as const, label: "Repository & Artifacts", icon: Archive }, { id: "evaluation" as const, label: "Evaluation & Reports", icon: Gauge }] },
  { label: "PLATFORM", items: [{ id: "governance" as const, label: "Governance", icon: LockKeyhole }, { id: "observability" as const, label: "Observability", icon: Activity }, { id: "pillars" as const, label: "Product Pillars", icon: Layers3 }, { id: "developer" as const, label: "Developer Tools", icon: FileJson }] }
];

const RUNTIME_COMPONENTS = [
  { name: "AICodeOrchestrator", type: "Root supervisor", manager: "Root", detail: "Menjadwalkan role repository, engineering, dan quality; menyimpan state, hasil, dan evidence." },
  { name: "Analyst", type: "WorkerRole.ANALYST", manager: "Repository manager", detail: "Menganalisis repository, struktur kode, konteks, dan kebutuhan task." },
  { name: "Architect", type: "WorkerRole.ARCHITECT", manager: "Repository manager", detail: "Menentukan pendekatan teknis dan dependency sebelum implementasi." },
  { name: "Developer", type: "WorkerRole.DEVELOPER", manager: "Engineering manager", detail: "Melakukan perubahan source pada workspace task sesuai policy." },
  { name: "Debugger", type: "WorkerRole.DEBUGGER", manager: "Engineering manager", detail: "Menganalisis kegagalan dan menjalankan repair cycle terbatas." },
  { name: "Tester", type: "WorkerRole.TESTER", manager: "Quality manager", detail: "Menjalankan pemeriksaan kualitas dan melaporkan evidence." },
  { name: "Security", type: "WorkerRole.SECURITY", manager: "Quality manager", detail: "Memeriksa policy dan evidence keamanan." }
];

type ArchitectureAgent = {
  name: string; priority: string; objective: string; responsibilities: string[];
  inputs: string[]; outputs: string[]; guardrails: string[]; extra?: string;
};

const ARCHITECTURE_AGENTS: ArchitectureAgent[] = [
  {
    name: "Supreme Orchestrator", priority: "Critical",
    objective: "Mengubah permintaan pengguna menjadi pekerjaan yang terencana, terdelegasi, terkontrol, terverifikasi, dan menghasilkan output nyata.",
    responsibilities: ["Memahami tujuan dan kebutuhan sistem", "Memanggil Planner dan Task Decomposer", "Memilih agent serta mengatur dependensi", "Memantau proses, retry, fallback, dan eskalasi", "Meminta verifikasi QA dan menghasilkan final report"],
    inputs: ["User request dan conversation context", "Project context dan system state", "Agent registry dan workflow status", "Policy constraints"],
    outputs: ["task_id dan execution_plan", "assigned_agents dan task_status", "validation_result dan evidence", "unresolved_issues dan final_report"],
    guardrails: ["Tidak melewati Policy Engine", "Tidak mengakses secret secara langsung", "Tidak menganggap pekerjaan selesai tanpa bukti", "Tidak menyetujui tindakan yang membutuhkan persetujuan manusia", "Tidak mengubah kebijakan keamanan sendiri"]
  },
  {
    name: "System Architect", priority: "Critical",
    objective: "Membangun blueprint teknis yang menjadi acuan pengembangan seluruh ekosistem RUSA AI.",
    responsibilities: ["Merancang service, agent, API, database, dan model architecture", "Menentukan komunikasi antarservice dan event-driven workflow", "Merancang memory dan integration architecture", "Mengidentifikasi technical debt", "Menentukan standar pengembangan dan migration plan"],
    inputs: ["User dan product requirements", "Existing repository", "Infrastructure specifications", "Technology constraints dan security requirements"],
    outputs: ["Architecture documentation dan system diagrams", "Component specifications dan database schemas", "API contracts dan integration specifications", "Technical recommendations dan migration plans"],
    guardrails: ["Tidak mengubah production tanpa izin", "Tidak mengabaikan security requirements", "Tidak menambah dependency tanpa evaluasi", "Tidak merancang komponen tanpa kontrak", "Tidak mengklaim desain diterapkan sebelum diverifikasi"]
  },
  {
    name: "Planner", priority: "High",
    objective: "Menghasilkan rencana yang realistis, terukur, memiliki prioritas, dan dapat dieksekusi.",
    responsibilities: ["Menganalisis tujuan dan kebutuhan", "Menentukan prioritas dan milestone", "Mengestimasi kompleksitas dengan label estimasi", "Mengidentifikasi risiko dan dependency", "Menyiapkan serta memperbarui execution roadmap"],
    inputs: ["User objectives", "System architecture dan project status", "Available resources dan agent capabilities", "Technical constraints"],
    outputs: ["Strategic plan dan implementation roadmap", "Milestone list dan priority matrix", "Dependency map dan risk register", "Completion criteria"],
    guardrails: ["Tidak mengubah kode", "Tidak menjalankan deployment", "Tidak menyajikan estimasi sebagai fakta", "Tidak mengabaikan dependency", "Tidak menyatakan target tercapai hanya berdasarkan rencana"]
  },
  {
    name: "Task Decomposer", priority: "High",
    objective: "Menghasilkan struktur pekerjaan yang jelas, terukur, tidak tumpang tindih, dan dapat dieksekusi paralel maupun berurutan.",
    responsibilities: ["Memecah tujuan menjadi subtasks", "Menentukan dependency dan parallel groups", "Mengidentifikasi blocking tasks", "Mengusulkan agent pelaksana", "Menentukan input, output, dan acceptance criteria setiap task"],
    inputs: ["Approved execution plan", "Project requirements", "Agent registry dan available capabilities", "System dependencies"],
    outputs: ["Task breakdown structure", "Dependency graph", "Agent assignment proposal", "Parallel execution groups", "Task acceptance criteria"],
    guardrails: ["Tidak mengubah prioritas P0/P1 tanpa persetujuan Orchestrator", "Tidak membuat task tanpa output", "Tidak mengabaikan dependency", "Tidak menugaskan agent tanpa capability", "Tidak menandai selesai tanpa evidence"]
  },
  {
    name: "Software Engineer", priority: "High",
    objective: "Menghasilkan kode yang berfungsi, aman, teruji, terdokumentasi, dan dapat dipelihara.",
    responsibilities: ["Repository inspection dan code generation", "Bug fixing, backend, frontend, API, dan database work", "Service integration dan refactoring", "Unit testing dan dokumentasi", "Menyiapkan implementation summary dan pull request"],
    inputs: ["Assigned task dan repository context", "Architecture specifications dan API contracts", "Coding standards dan test requirements", "Security requirements"],
    outputs: ["Source code changes dan test files", "Documentation dan migration files", "Pull request", "Implementation summary dan known limitations"],
    guardrails: ["Tidak memasukkan secret ke repository", "Tidak melewati CI", "Tidak menghapus data production", "Tidak mengubah branch protection", "Tidak merge PR sendiri tanpa otorisasi", "Tidak mengklaim test berhasil tanpa menjalankan test"]
  },
  {
    name: "DevOps", priority: "High",
    objective: "Memastikan RUSA AI berjalan stabil, aman, terukur, dan berkelanjutan pada infrastruktur yang digunakan.",
    responsibilities: ["VPS dan Linux administration", "Docker dan container orchestration", "CI/CD dan application deployment", "Reverse proxy dan database operations", "Backup, recovery, monitoring, logging, dan resource optimization"],
    inputs: ["Approved deployment task", "Infrastructure dan environment configuration", "Application artifacts dan deployment instructions", "Security requirements"],
    outputs: ["Deployment status dan infrastructure configuration", "Service health dan container report", "Deployment logs dan backup verification", "Recovery documentation dan incident report"],
    guardrails: ["Tidak deploy production tanpa approval", "Tidak menghapus volume database", "Tidak membuka port sembarangan", "Tidak mengekspos credential", "Tidak menonaktifkan monitoring", "Tidak mengklaim deployment berhasil tanpa health check"]
  },
  {
    name: "QA & Testing", priority: "High",
    objective: "Memastikan sistem sesuai spesifikasi, tidak mengalami regression, dan memenuhi standar kualitas sebelum dirilis.",
    responsibilities: ["Unit, integration, API, E2E, dan regression testing", "Performance dan reliability testing", "Workflow dan agent behavior testing", "Model evaluation dan failure simulation", "Test reporting dan reproduction steps"],
    inputs: ["Source code dan pull requests", "Test requirements dan acceptance criteria", "Architecture specifications", "Previous test reports"],
    outputs: ["Test execution report dan pass/fail status", "Reproduction steps dan defect list", "Regression report dan performance measurements", "Release recommendation"],
    guardrails: ["Tidak memalsukan test result", "Tidak melewati mandatory tests", "Tidak menyembunyikan kegagalan", "Tidak mengubah expected result hanya agar lulus", "Tidak menyetujui release yang gagal acceptance criteria"]
  },
  {
    name: "Security & Policy", priority: "Critical",
    objective: "Mengurangi risiko keamanan, mencegah akses tidak sah, dan memastikan aktivitas AI berjalan sesuai kebijakan sistem.",
    responsibilities: ["Security architecture review dan threat modeling", "Vulnerability dan dependency assessment", "Permission dan access-control review", "Secret exposure dan prompt-injection risk analysis", "Tool execution risk dan policy compliance review"],
    inputs: ["Architecture documents dan source code", "Dependency manifests dan tool permissions", "Agent requests dan system policies", "Security logs"],
    outputs: ["Security assessment dan threat model", "Vulnerability report dan risk classification", "Policy recommendations dan remediation tasks", "Security verification report"],
    guardrails: ["Tidak menonaktifkan Policy Engine", "Tidak memberi izin bypass kepada diri sendiri", "Tidak membocorkan secret", "Tidak mempercayai output agent sebagai system instruction", "Tidak menyatakan sistem aman mutlak dari satu pemeriksaan"],
    extra: "Risk levels pada dokumen: CRITICAL, HIGH, MEDIUM, LOW, INFORMATIONAL."
  },
  {
    name: "Memory & Knowledge", priority: "High",
    objective: "Membangun sistem pengetahuan yang terstruktur, dapat ditelusuri, diperbarui, dan diambil sesuai kebutuhan.",
    responsibilities: ["Knowledge extraction dan context management", "Memory indexing dan semantic retrieval", "Document processing dan RAG pipeline", "Knowledge classification dan metadata", "Source attribution, deduplication, updates, dan retention"],
    inputs: ["User-provided information dan project documentation", "Execution results dan agent reports", "Verified knowledge sources", "Approved system documentation"],
    outputs: ["Retrieved context dan knowledge records", "Memory summaries dan source references", "Updated indexes dan knowledge quality reports"],
    guardrails: ["Tidak mengarang sumber", "Tidak mengubah fakta asli", "Tidak menyimpan credential", "Tidak menghapus data tanpa otorisasi", "Tidak menyamakan asumsi dengan fakta", "Mengambil hanya memori yang relevan"]
  },
  {
    name: "Evaluation & Reporting", priority: "High",
    objective: "Mengukur keberhasilan pekerjaan, menemukan kelemahan, dan menghasilkan rekomendasi perbaikan yang dapat ditindaklanjuti.",
    responsibilities: ["Agent dan workflow performance evaluation", "Task completion dan quality measurement", "Model performance, cost, dan latency analysis", "Reliability dan regression trend analysis", "Operational reporting dan improvement recommendations"],
    inputs: ["Workflow logs dan agent execution records", "QA reports dan infrastructure metrics", "Model evaluation results dan task outcomes", "Audit records"],
    outputs: ["Performance dashboard dan agent scorecards", "Evaluation reports dan failure analysis", "Cost reports dan quality trends", "Improvement recommendations dan regression reports"],
    guardrails: ["Tidak memalsukan metrik", "Tidak mengubah log historis", "Tidak menghapus kegagalan", "Tidak mengubah production agent otomatis tanpa approval", "Tidak menyimpulkan tanpa data memadai", "Tidak menggunakan satu metrik sebagai ukuran tunggal"],
    extra: "KPI: completion/failure/retry rate, task duration, blocked count, agent success, output validity, tool accuracy, policy violations, escalation, CPU/RAM/storage/network/availability, response quality, groundedness, hallucination, instruction adherence."
  }
];

const DEVOPS_STAGES = [
  { id: "rusa_plan_agent", name: "Plan", purpose: "Mengubah kebutuhan menjadi rencana, task, dependency, acceptance criteria, dan risk assessment.", inputs: ["User request atau product backlog", "Dokumentasi arsitektur", "Hasil Monitor dan Operate", "Riwayat Repository"], outputs: ["plan.md", "requirements.md", "backlog.yaml", "acceptance-criteria.md", "risk-assessment.md"], exit: ["Scope jelas", "Acceptance criteria dapat diverifikasi", "Dependency dan risiko dicatat"], boundary: "Tidak mengubah scope produk yang disetujui tanpa persetujuan pemilik produk." },
  { id: "rusa_code_agent", name: "Code", purpose: "Mengimplementasikan perubahan pada branch/worktree khusus sesuai task dan kontrak.", inputs: ["Task dan acceptance criteria", "Source code dan konfigurasi", "Coding standards dan API contract", "Review atau test sebelumnya"], outputs: ["Source code pada branch khusus", "Unit test dan dokumentasi", "Commit dan pull request", "code-change-summary.md"], exit: ["Perubahan sesuai acceptance criteria", "Test relevan tersedia", "Tidak ada secret di repository", "PR dapat ditinjau"], boundary: "Tidak force-push ke main, mengubah branch protection, merge PR sendiri, atau mengubah secret produksi." },
  { id: "rusa_build_agent", name: "Build", purpose: "Menghasilkan artefak perangkat lunak yang konsisten, reproducible, dan dapat ditelusuri.", inputs: ["Source commit atau PR head", "Build configuration dan lockfile", "Target platform/runtime", "Build policy"], outputs: ["Binary/package/container image", "Build log dan SHA-256", "Artifact manifest dan SBOM bila didukung", "build-report.md"], exit: ["Build tanpa error", "Artefak terikat commit dan versi", "Checksum tersedia", "Dependency tercatat"], boundary: "Tidak mengganti dependency atau base image secara diam-diam untuk mengakali kegagalan." },
  { id: "rusa_test_agent", name: "Test", purpose: "Memverifikasi source dan build terhadap acceptance criteria serta security policy.", inputs: ["Source commit dan build artifacts", "Test plan dan acceptance criteria", "Test environment dan security policy"], outputs: ["Test/coverage/regression report", "Security findings", "test-evidence.json", "PASS, FAIL, atau BLOCKED"], exit: ["Required tests selesai", "Tidak ada critical failure terbuka", "Evidence terikat commit yang tepat", "Hasil reproducible"], boundary: "Tidak boleh menyatakan PASS jika test tidak dijalankan, tidak lengkap, atau memakai evidence commit berbeda." },
  { id: "rusa_release_agent", name: "Release", purpose: "Menyiapkan release candidate setelah build, test, provenance, dan release gates diverifikasi.", inputs: ["Build manifest dan test evidence", "Pull request dan commit", "Security findings dan migration notes", "Release policy"], outputs: ["Release candidate dan changelog", "Release manifest", "Deployment/rollback notes", "release-readiness-report.md"], exit: ["Required gate lulus", "Blocker terselesaikan", "Artefak diverifikasi", "Approval tersedia jika diwajibkan"], boundary: "Dapat menyiapkan release candidate; publikasi rilis resmi mengikuti approval repository." },
  { id: "rusa_deploy_agent", name: "Deploy", purpose: "Menerapkan release yang disetujui ke environment dan memverifikasi hasilnya.", inputs: ["Approved release manifest dan image digest", "Environment configuration", "Deployment policy", "Migration/rollback plan dan approval record"], outputs: ["Deployment record dan versi aktif", "Health check dan smoke test result", "Rollback record bila diperlukan"], exit: ["Versi sesuai manifest", "Health dan smoke test lulus", "Tidak ada config change tanpa approval", "Deployment tercatat"], boundary: "Staging dapat otomatis sesuai policy; production wajib approval. Secret mentah tidak boleh ditampilkan." },
  { id: "rusa_operate_agent", name: "Operate", purpose: "Menjaga operasi layanan, melakukan triage, menjalankan recovery yang diizinkan, dan mencatat insiden.", inputs: ["Alert dan health check", "Log, trace, dan runbook", "Incident history dan operational policy"], outputs: ["Operational report dan incident timeline", "Recovery record dan capacity report", "incident-report.md"], exit: ["Insiden terklasifikasi", "Tindakan sesuai runbook", "Kondisi layanan diverifikasi", "Masalah tersisa dieskalasikan"], boundary: "Hanya menjalankan tindakan allowlisted; tindakan destruktif dan perubahan infrastruktur besar memerlukan approval." },
  { id: "rusa_monitor_agent", name: "Monitor", purpose: "Mengumpulkan telemetry, membandingkan SLO/threshold, mendeteksi anomali, dan mengirim alert.", inputs: ["Metrics, logs, traces", "SLO dan alert rules", "Deployment events dan incident history"], outputs: ["Monitoring dashboard data dan alert", "Anomaly/SLO report dan capacity trend", "monitoring-insight.md"], exit: ["Data memiliki timestamp", "Alert punya severity dan konteks", "Temuan terhubung ke service/version", "Rekomendasi punya evidence"], boundary: "Read-only secara default; tidak mengubah config produksi, restart, atau deploy tanpa mandat eksplisit." }
];

const MEMORY_TYPES = [
  { name: "Working Memory", detail: "Informasi untuk pekerjaan aktif.", purpose: "Context sementara untuk run/task yang sedang berjalan." },
  { name: "Episodic Memory", detail: "Riwayat pekerjaan dan hasil eksekusi.", purpose: "Rekam pengalaman, outcome, dan urutan kejadian." },
  { name: "Semantic Memory", detail: "Fakta dan pengetahuan terstruktur.", purpose: "Menyimpan pengetahuan yang dapat diambil berdasarkan makna." },
  { name: "Procedural Memory", detail: "Prosedur dan pengalaman kerja.", purpose: "Mendukung penggunaan runbook dan prosedur terverifikasi." },
  { name: "Project Memory", detail: "Informasi khusus masing-masing proyek.", purpose: "Menjaga konteks proyek tetap terpisah dan relevan." }
];

const KPI_GROUPS = [
  { name: "Task Metrics", items: ["Completion rate", "Failure rate", "Retry rate", "Task duration", "Blocked task count"] },
  { name: "Agent Metrics", items: ["Success rate", "Output validity", "Tool execution accuracy", "Policy violation count", "Escalation frequency"] },
  { name: "Infrastructure Metrics", items: ["CPU utilization", "RAM utilization", "Storage utilization", "Network performance", "Service availability"] },
  { name: "AI Metrics", items: ["Response quality", "Groundedness", "Hallucination rate", "Instruction adherence", "Evaluation score"] }
];

const PILLARS = [
  { name: "AI Autonomous Software Engineering & DevOps", status: "Partial integration", detail: "AICode task intake, orchestrator, worker pipeline, task state, retry, cancellation, dan evidence payload.", evidence: "FastAPI /v1/aicode/tasks dan rusa_agents runtime." },
  { name: "RUSA Intelligence", status: "Partial integration", detail: "Inference API dan model discovery tersedia. Memory, knowledge retrieval, dan agent evaluation belum diekspos pada web API.", evidence: "FastAPI /v1/chat/completions dan /v1/models." },
  { name: "RUSA Business", status: "Architecture defined", detail: "Business operations automation, research, dan company analysis belum memiliki UI/API pada scope repository yang diperiksa.", evidence: "RUSA_AI_Architecture.md." },
  { name: "RUSA Ecosystem", status: "Architecture defined", detail: "Marketplace, developer API, external integrations, dan agent ecosystem belum memiliki UI/API pada scope repository yang diperiksa.", evidence: "RUSA_AI_Architecture.md." }
];

const ORCHESTRATOR_STEPS = ["RECEIVE", "UNDERSTAND", "PLAN", "DECOMPOSE", "DELEGATE", "EXECUTE", "VALIDATE", "REVIEW", "COMPLETE", "LEARN"];
const ROLE_PERMISSIONS: Record<string, string[]> = {
  platform_admin: ["task:create", "task:read", "task:retry", "task:cancel", "operator metrics"],
  tenant_admin: ["task:create", "task:read", "task:retry", "task:cancel"],
  developer: ["task:create", "task:read", "inference"],
  read_only: ["task:read"]
};
const PROTECTED_ACTIONS = ["merge", "deploy", "release", "secret_change", "permission_change", "destructive_action", "production_write"];
const DENIED_ACTIONS = ["disable_audit", "exfiltrate_secret", "force_push_main"];

const DEFAULT_ENVELOPE = JSON.stringify({
  schema_version: 1,
  type: "task.request",
  message_id: "msg-721",
  tenant_id: "tenant-a",
  run_id: "run-42",
  task_id: "task-07",
  attempt: 1,
  correlation_id: "corr-42",
  traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  producer: "orchestrator",
  occurred_at: "2026-11-06T11:00:00Z",
  payload: {
    capability: "research",
    input_ref: "artifact-31-v1",
    deadline_at: "2026-11-06T11:02:00Z",
    idempotency_key: "run-42/task-07/1",
    priority: "P2"
  }
}, null, 2);

function errorText(data: unknown): string {
  if (data && typeof data === "object") {
    const item = data as Record<string, unknown>;
    if (typeof item.detail === "string") return item.detail;
    if (typeof item.error === "string") return item.error;
    if (typeof item.message === "string") return item.message;
  }
  return "Request could not be completed.";
}
function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("id-ID", { dateStyle: "medium", timeStyle: "short" });
}
function taskObjective(task: Task): string {
  return task.payload?.objective || "Untitled task";
}
function statusTone(status: string): string {
  if (["succeeded", "ready", "healthy"].includes(status)) return "success";
  if (["running", "queued", "awaiting_approval", "warning"].includes(status)) return "warning";
  if (["failed", "blocked", "unavailable", "error"].includes(status)) return "danger";
  return "neutral";
}
function statusLabel(status: string): string {
  return (status || "unknown").replaceAll("_", " ");
}
function resultObject(task: Task): Record<string, unknown> {
  return task.result && typeof task.result === "object" ? task.result : {};
}
function extractArtifacts(task: Task): string[] {
  const result = resultObject(task);
  const values = result.artifacts;
  if (Array.isArray(values)) return values.filter((item): item is string => typeof item === "string");
  return [];
}
function extractEvidence(task: Task): Record<string, unknown> {
  const result = resultObject(task);
  return result.evidence && typeof result.evidence === "object" ? result.evidence as Record<string, unknown> : {};
}
function jsonText(value: unknown): string {
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: string }) {
  return <span className={"rc-badge rc-badge-" + tone}>{children}</span>;
}
function Panel({ title, subtitle, action, children, className = "" }: { title: string; subtitle?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={"rc-panel " + className}>
      <div className="rc-panel-head">
        <div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>
        {action}
      </div>
      {children}
    </section>
  );
}
function StatCard({ label, value, detail, icon: Icon, tone = "cyan" }: { label: string; value: string | number; detail: string; icon: typeof Activity; tone?: string }) {
  return (
    <div className="rc-stat">
      <div className="rc-stat-top"><span>{label}</span><span className={"rc-stat-icon rc-stat-" + tone}><Icon size={17} /></span></div>
      <strong>{value}</strong><small>{detail}</small>
    </div>
  );
}
function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="rc-empty"><CircleDot size={20} /><strong>{title}</strong><span>{detail}</span></div>;
}
function MetricPreview({ text }: { text: string }) {
  const lines = text.split(/\r?\n/).filter((line) => line && !line.startsWith("#")).slice(0, 18);
  if (!lines.length) return <EmptyState title="No metric samples" detail="Endpoint metrics belum mengembalikan sample." />;
  return <div className="rc-metric-list">{lines.map((line, index) => <div className="rc-metric-line" key={index}><code>{line}</code></div>)}</div>;
}

export default function RusaControlCenter() {
  const [session, setSession] = useState<Identity | null>(null);
  const [booting, setBooting] = useState(true);
  const [apiKey, setApiKey] = useState("");
  const [loginError, setLoginError] = useState("");
  const [page, setPage] = useState<PageId>("overview");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskTotal, setTaskTotal] = useState(0);
  const [taskOffset, setTaskOffset] = useState(0);
  const [taskFilter, setTaskFilter] = useState<TaskFilter>("all");
  const [taskSearch, setTaskSearch] = useState("");
  const [approvalTasks, setApprovalTasks] = useState<Task[]>([]);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);

  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [objective, setObjective] = useState("");
  const [context, setContext] = useState("");
  const [repository, setRepository] = useState("StrategyG/RusaAi");
  const [baseBranch, setBaseBranch] = useState("main");
  const [criteria, setCriteria] = useState("Implement the requested change\nAdd or update relevant tests\nPreserve tenant isolation and policy controls");

  const [health, setHealth] = useState<Health | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [modelError, setModelError] = useState("");
  const [metricsText, setMetricsText] = useState("");
  const [metricsFetchedAt, setMetricsFetchedAt] = useState("");
  const [metricsError, setMetricsError] = useState("");

  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatStream, setChatStream] = useState(true);
  const [chatModel, setChatModel] = useState("");
  const [chatMessages, setChatMessages] = useState<ChatLine[]>([
    { role: "assistant", content: "RUSA AI siap menerima pertanyaan. Chat menggunakan inference API; pekerjaan coding dijalankan melalui AICode Tasks." }
  ]);

  const [activeAgent, setActiveAgent] = useState<ArchitectureAgent | null>(null);
  const [activeStage, setActiveStage] = useState<(typeof DEVOPS_STAGES)[number] | null>(DEVOPS_STAGES[0]);
  const [envelopeText, setEnvelopeText] = useState(DEFAULT_ENVELOPE);
  const [validation, setValidation] = useState<{ valid: boolean; errors: string[] } | null>(null);
  const [validationError, setValidationError] = useState("");
  const [validationBusy, setValidationBusy] = useState(false);

  const canCreate = session?.roles.some((role) => ["platform_admin", "tenant_admin", "developer"].includes(role)) ?? false;
  const canControl = session?.roles.some((role) => ["platform_admin", "tenant_admin"].includes(role)) ?? false;
  const canUseChat = session?.roles.some((role) => ["platform_admin", "tenant_admin", "developer"].includes(role)) ?? false;
  const isPlatformAdmin = session?.roles.includes("platform_admin") ?? false;

  const refreshTasks = useCallback(async (offset = 0, filter: TaskFilter = "all") => {
    const params = new URLSearchParams({ limit: "25", offset: String(offset) });
    if (filter !== "all" && filter !== "active") params.set("status", filter);
    try {
      const response = await fetch("/api/tasks?" + params.toString(), { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      const result = data as TaskPage;
      setTasks(Array.isArray(result.items) ? result.items : []);
      setTaskTotal(typeof result.total === "number" ? result.total : 0);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not refresh task queue.");
    }
  }, []);

  const refreshApprovals = useCallback(async () => {
    try {
      const response = await fetch("/api/tasks?status=awaiting_approval&limit=100&offset=0", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (response.ok) setApprovalTasks(Array.isArray(data.items) ? data.items : []);
    } catch {
      // The main task list remains available even if the approval projection cannot be refreshed.
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (response.ok) setHealth(await response.json());
      else setHealth(null);
    } catch { setHealth(null); }
  }, []);

  const refreshModels = useCallback(async () => {
    try {
      const response = await fetch("/api/models", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      const ids = Array.isArray(data.data) ? data.data.map((item: { id?: unknown }) => item.id).filter((id: unknown): id is string => typeof id === "string") : [];
      setModels(ids);
      setChatModel((current) => current || ids[0] || "");
      setModelError("");
    } catch (cause) {
      setModels([]);
      setModelError(cause instanceof Error ? cause.message : "Model discovery unavailable.");
    }
  }, []);

  const refreshMetrics = useCallback(async () => {
    if (!isPlatformAdmin) return;
    setMetricsError("");
    try {
      const response = await fetch("/api/metrics", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setMetricsText(typeof data.text === "string" ? data.text : "");
      setMetricsFetchedAt(typeof data.fetched_at === "string" ? data.fetched_at : "");
    } catch (cause) {
      setMetricsError(cause instanceof Error ? cause.message : "Metrics endpoint unavailable.");
      setMetricsText("");
    }
  }, [isPlatformAdmin]);

  const loadSession = useCallback(async () => {
    try {
      const response = await fetch("/api/session", { cache: "no-store" });
      if (response.ok) {
        const identity = await response.json();
        setSession(identity);
        await Promise.all([refreshTasks(0, "all"), refreshApprovals(), refreshHealth(), refreshModels()]);
      }
    } catch {
      setLoginError("RUSA API belum dapat dijangkau.");
    } finally {
      setBooting(false);
    }
  }, [refreshTasks, refreshApprovals, refreshHealth, refreshModels]);

  useEffect(() => { void loadSession(); }, [loadSession]);

  useEffect(() => {
    if (!session) return;
    const taskTimer = window.setInterval(() => {
      void refreshTasks(taskOffset, taskFilter);
      void refreshApprovals();
    }, 10000);
    const healthTimer = window.setInterval(() => { void refreshHealth(); }, 30000);
    return () => { window.clearInterval(taskTimer); window.clearInterval(healthTimer); };
  }, [session, taskOffset, taskFilter, refreshTasks, refreshApprovals, refreshHealth]);

  useEffect(() => {
    if (session && page === "observability" && isPlatformAdmin) void refreshMetrics();
  }, [session, page, isPlatformAdmin, refreshMetrics]);

  const counts = useMemo(() => ({
    active: tasks.filter((task) => ["queued", "running"].includes(task.status)).length,
    failed: tasks.filter((task) => ["failed", "blocked"].includes(task.status)).length,
    succeeded: tasks.filter((task) => task.status === "succeeded").length
  }), [tasks]);

  const visibleTasks = useMemo(() => {
    let result = tasks;
    if (taskFilter === "active") result = result.filter((task) => ["queued", "running"].includes(task.status));
    const query = taskSearch.trim().toLowerCase();
    if (query) result = result.filter((task) =>
      taskObjective(task).toLowerCase().includes(query)
      || task.task_id.toLowerCase().includes(query)
      || (task.payload?.repository?.full_name || "").toLowerCase().includes(query)
    );
    return result;
  }, [tasks, taskFilter, taskSearch]);

  const loadIdentityAndData = async (identity: Identity) => {
    setSession(identity);
    await Promise.all([refreshTasks(0, "all"), refreshApprovals(), refreshHealth(), refreshModels()]);
  };

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setLoginError("");
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apiKey })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setApiKey("");
      await loadIdentityAndData(data as Identity);
    } catch (cause) {
      setLoginError(cause instanceof Error ? cause.message : "Login failed.");
    } finally { setBusy(false); }
  }

  async function logout() {
    await fetch("/api/session", { method: "DELETE" });
    setSession(null);
    setTasks([]);
    setApprovalTasks([]);
    setTaskTotal(0);
    setSelectedTask(null);
    setChatMessages([{ role: "assistant", content: "Sesi berakhir. Hubungkan kembali untuk memulai." }]);
    setPage("overview");
  }

  async function openTask(taskId: string) {
    setError("");
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId), { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setSelectedTask(data as Task);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load task details.");
    }
  }

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const acceptanceCriteria = criteria.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    if (!objective.trim() || objective.trim().length > 8000) {
      setError("Objective wajib diisi dan maksimal 8.000 karakter.");
      return;
    }
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository.trim())) {
      setError("Repository harus menggunakan format owner/name.");
      return;
    }
    if (!baseBranch.trim() || baseBranch.length > 181 || baseBranch.includes("..") || baseBranch.includes("//") || baseBranch.endsWith("/")) {
      setError("Base branch tidak valid.");
      return;
    }
    if (!acceptanceCriteria.length || acceptanceCriteria.length > 30 || acceptanceCriteria.some((item) => item.length > 1000)) {
      setError("Acceptance criteria harus berisi 1–30 item, masing-masing maksimal 1.000 karakter.");
      return;
    }
    if (context.length > 20000) {
      setError("Context maksimal 20.000 karakter.");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          objective: objective.trim(),
          context,
          repository: { full_name: repository.trim(), base_branch: baseBranch.trim() },
          acceptance_criteria: acceptanceCriteria
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNewTaskOpen(false);
      setObjective("");
      setContext("");
      setCriteria("Implement the requested change\nAdd or update relevant tests\nPreserve tenant isolation and policy controls");
      setNotice("Task diterima: " + data.task_id + " · status " + data.status + ".");
      setPage("tasks");
      await refreshTasks(0, "all");
      if (data.task_id) await openTask(data.task_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Task could not be created.");
    } finally { setBusy(false); }
  }

  async function cancelTask(taskId: string) {
    if (!canControl || !window.confirm("Kirim permintaan pembatalan task? Operasi eksternal yang sudah berjalan mungkin memerlukan rekonsiliasi.")) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId), { method: "DELETE" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNotice("Permintaan pembatalan diterima backend.");
      await Promise.all([refreshTasks(taskOffset, taskFilter), refreshApprovals()]);
      await openTask(taskId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Cancel failed.");
    } finally { setBusy(false); }
  }

  async function retryTask(taskId: string) {
    if (!canControl || !window.confirm("Retry hanya untuk task berstatus failed. Pastikan penyebab kegagalan telah ditinjau.")) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId) + "/retry", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNotice("Task diterima kembali oleh antrean.");
      await refreshTasks(taskOffset, taskFilter);
      await openTask(taskId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Retry failed.");
    } finally { setBusy(false); }
  }

  async function sendChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = chatInput.trim();
    if (!prompt || chatBusy || !canUseChat) return;
    const nextMessages = [...chatMessages, { role: "user" as const, content: prompt }];
    setChatMessages(nextMessages);
    setChatInput("");
    setChatBusy(true);
    let placeholderAdded = false;
    const updateAssistant = (content: string) => {
      setChatMessages((current) => {
        const next = [...current];
        const index = next.length - 1;
        if (index >= 0 && next[index].role === "assistant") next[index] = { role: "assistant", content };
        return next;
      });
    };
    try {
      const body = {
        ...(chatModel ? { model: chatModel } : {}),
        messages: nextMessages.map((item) => ({ role: item.role, content: item.content })),
        temperature: 0.7,
        top_p: 0.9,
        max_tokens: 2048,
        stream: chatStream
      };
      let response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (chatStream && response.status === 501) {
        response = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, stream: false })
        });
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorText(data));
      }
      const contentType = response.headers.get("content-type") || "";
      if (chatStream && contentType.includes("text/event-stream")) {
        if (!response.body) throw new Error("Streaming response body is unavailable.");
        setChatMessages((current) => [...current, { role: "assistant", content: "" }]);
        placeholderAdded = true;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let output = "";
        let finished = false;
        const consumeEvent = (event: string) => {
          const dataLine = event.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
          if (!dataLine || dataLine === "[DONE]") { if (dataLine === "[DONE]") finished = true; return; }
          let parsed: Record<string, unknown>;
          try { parsed = JSON.parse(dataLine) as Record<string, unknown>; } catch { return; }
          if (parsed.error && typeof parsed.error === "object") {
            const message = (parsed.error as Record<string, unknown>).message;
            throw new Error(typeof message === "string" ? message : "Streaming inference failed.");
          }
          const choices = Array.isArray(parsed.choices) ? parsed.choices : [];
          const choice = choices[0] as Record<string, unknown> | undefined;
          const delta = choice?.delta as Record<string, unknown> | undefined;
          if (typeof delta?.content === "string") {
            output += delta.content;
            updateAssistant(output);
          }
        };
        while (!finished) {
          const chunk = await reader.read();
          buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
          const events = buffer.split(/\r?\n\r?\n/);
          buffer = events.pop() || "";
          for (const item of events) consumeEvent(item);
          if (chunk.done) break;
        }
        if (buffer.trim()) consumeEvent(buffer);
        if (!output.trim()) updateAssistant("Model mengembalikan respons kosong.");
      } else {
        const data = await response.json().catch(() => ({}));
        const answer = (data as { choices?: Array<{ message?: { content?: string } }> }).choices?.[0]?.message?.content || "Model returned an empty response.";
        setChatMessages((current) => [...current, { role: "assistant", content: answer }]);
      }
    } catch (cause) {
      const message = "Request gagal: " + (cause instanceof Error ? cause.message : "Unknown error");
      if (placeholderAdded) updateAssistant(message);
      else setChatMessages((current) => [...current, { role: "assistant", content: message }]);
    } finally { setChatBusy(false); }
  }

  async function validateEnvelope(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidation(null);
    setValidationError("");
    let envelope: unknown;
    try { envelope = JSON.parse(envelopeText); }
    catch { setValidationError("JSON tidak valid. Periksa tanda kutip, koma, dan struktur object."); return; }
    if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
      setValidationError("Envelope harus berupa JSON object.");
      return;
    }
    setValidationBusy(true);
    try {
      const response = await fetch("/api/validate-task", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ envelope })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setValidation(data as { valid: boolean; errors: string[] });
    } catch (cause) {
      setValidationError(cause instanceof Error ? cause.message : "Validation request failed.");
    } finally { setValidationBusy(false); }
  }

  const pageInfo = PAGE_META[page];
  const livenessOk = health?.liveness.ok ?? false;
  const readinessOk = health?.readiness.ok ?? false;
  const loadedCount = tasks.length;
  const pageCount = Math.max(1, Math.ceil(taskTotal / 25));
  const currentPage = Math.floor(taskOffset / 25) + 1;
  const artifacts = tasks.flatMap((task) => extractArtifacts(task).map((artifact) => ({ task, artifact })));
  const evidenceItems = tasks.flatMap((task) => Object.entries(extractEvidence(task)).map(([key, value]) => ({ task, key, value })));

  if (booting) return <main className="rc-loading"><div className="rc-brand-mark"><BrainCircuit size={25} /></div><strong>RUSA AI</strong><span>Memuat secure workspace…</span></main>;

  if (!session) {
    return (
      <main className="rc-login-wrap">
        <section className="rc-login-card">
          <div className="rc-brand"><div className="rc-brand-mark"><BrainCircuit size={25} /></div><div><strong>RUSA AI</strong><small>AGENTIC CONTROL CENTER</small></div></div>
          <Badge tone="success">SECURE WORKSPACE ACCESS</Badge>
          <h1>Masuk ke RUSA AI</h1>
          <p>Gunakan tenant-scoped RUSA API key. Kunci disimpan dalam sesi server terenkripsi dan tidak dikirim kembali ke browser.</p>
          <form onSubmit={login} className="rc-form">
            <label>RUSA API Key<input type="password" autoComplete="current-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="Masukkan API key" required /></label>
            {loginError && <div className="rc-alert rc-alert-danger">{loginError}</div>}
            <button className="rc-btn rc-btn-primary rc-btn-wide" disabled={busy || !apiKey.trim()} type="submit">{busy ? "Menghubungkan…" : "Connect to RUSA AI"} <ArrowRight size={16} /></button>
          </form>
          <div className="rc-login-foot"><LockKeyhole size={15} /> Akses ditentukan oleh tenant, roles, dan permission RUSA API. Jangan gunakan API key development pada production.</div>
        </section>
      </main>
    );
  }

  return (
    <main className="rc-app">
      <div className="rc-shell">
        <aside className={"rc-sidebar" + (mobileOpen ? " rc-sidebar-open" : "")}>
          <div className="rc-brand"><div className="rc-brand-mark"><BrainCircuit size={22} /></div><div><strong>RUSA AI</strong><small>AGENTIC CONTROL CENTER</small></div></div>
          <div className="rc-tenant"><Command size={15} /><span>{session.tenant_id}</span><Badge tone="success">Authenticated</Badge></div>
          <nav className="rc-nav">
            {NAV_GROUPS.map((group) => (
              <div className="rc-nav-group" key={group.label}>
                <div className="rc-nav-label">{group.label}</div>
                {group.items.map((item) => {
                  const Icon = item.icon;
                  return (
                    <button key={item.id} className={"rc-nav-item" + (page === item.id ? " active" : "")} onClick={() => { setPage(item.id); setMobileOpen(false); }}>
                      <Icon size={17} /><span>{item.label}</span>
                      {item.id === "approvals" && approvalTasks.length > 0 && <b>{approvalTasks.length}</b>}
                    </button>
                  );
                })}
              </div>
            ))}
          </nav>
          <div className="rc-sidebar-foot">
            <div className="rc-connection"><span className="rc-live-dot" /> API session authenticated</div>
            <div className="rc-user-row"><div className="rc-avatar">{session.subject.slice(0, 1).toUpperCase()}</div><div className="rc-user-meta"><strong>{session.subject}</strong><small>{session.roles.join(" · ")}</small></div><button className="rc-icon-btn" onClick={logout} aria-label="Sign out" title="Sign out"><LogOut size={16} /></button></div>
          </div>
        </aside>

        <section className="rc-main">
          <header className="rc-topbar">
            <div className="rc-topbar-left"><button className="rc-icon-btn rc-mobile-menu" onClick={() => setMobileOpen((value) => !value)} aria-label="Toggle navigation"><Menu size={19} /></button><div className="rc-breadcrumb">RUSA AI <span>/</span> <strong>{pageInfo.title}</strong></div></div>
            <div className="rc-topbar-right"><span className="rc-role-pill">{session.roles.join(" · ")}</span><button className="rc-btn rc-btn-ghost" onClick={() => { void refreshTasks(taskOffset, taskFilter); void refreshApprovals(); void refreshHealth(); if (page === "observability") void refreshMetrics(); }}><RefreshCw size={14} /> Refresh</button></div>
          </header>

          <div className="rc-content">
            <div className="rc-page-head">
              <div><div className="rc-eyebrow">{pageInfo.eyebrow}</div><h1>{pageInfo.title}</h1><p>{pageInfo.description}</p></div>
              {canCreate && <button className="rc-btn rc-btn-primary" onClick={() => setNewTaskOpen(true)}><Plus size={16} /> New AICode Task</button>}
            </div>
            {error && <div className="rc-alert rc-alert-danger"><AlertTriangle size={16} />{error}<button onClick={() => setError("")} aria-label="Dismiss"><X size={14} /></button></div>}
            {notice && <div className="rc-alert rc-alert-success"><CheckCircle2 size={16} />{notice}<button onClick={() => setNotice("")} aria-label="Dismiss"><X size={14} /></button></div>}

            {page === "overview" && (
              <div className="rc-stack">
                <div className="rc-stat-grid">
                  <StatCard label="Total task records" value={taskTotal} detail="All records in authenticated tenant" icon={ListChecks} />
                  <StatCard label="Active in loaded page" value={counts.active} detail={"From " + loadedCount + " loaded records"} icon={Play} tone="blue" />
                  <StatCard label="Awaiting approval" value={approvalTasks.length} detail="Loaded approval queue · max 100" icon={ShieldAlert} tone="amber" />
                  <StatCard label="Failed / blocked sample" value={counts.failed} detail="From current task page" icon={AlertTriangle} tone="red" />
                </div>
                <div className="rc-grid-two">
                  <Panel title="Service readiness" subtitle="Health endpoints · refreshed every 30 seconds" action={<Badge tone={readinessOk ? "success" : "warning"}>{readinessOk ? "READY" : "NOT READY"}</Badge>}>
                    <div className="rc-health-list">
                      <div><span><CircleDot size={14} /> Liveness · GET /health</span><Badge tone={livenessOk ? "success" : "danger"}>{livenessOk ? "Healthy" : "Unavailable"}</Badge></div>
                      <div><span><CircleDot size={14} /> Readiness · GET /health/ready</span><Badge tone={readinessOk ? "success" : "danger"}>{readinessOk ? "Ready" : "Not ready"}</Badge></div>
                    </div>
                    {health?.readiness.data && <pre className="rc-code rc-code-compact">{jsonText(health.readiness.data)}</pre>}
                    <div className="rc-note">Readiness reflects inference and configured database/queue dependencies reported by the backend.</div>
                  </Panel>
                  <Panel title="AICode runtime topology" subtitle="Source-defined roles, not live agent heartbeats">
                    <div className="rc-runtime-summary"><div className="rc-runtime-number">7</div><div><strong>Runtime components in source</strong><span>1 orchestrator + 6 worker roles</span></div></div>
                    <div className="rc-chip-row">{["Repository", "Engineering", "Quality"].map((name) => <Badge key={name} tone="info">{name} manager</Badge>)}</div>
                    <button className="rc-text-btn" onClick={() => setPage("orchestrator")}>Inspect execution pipeline <ArrowRight size={14} /></button>
                  </Panel>
                </div>
                <Panel title="Recent AICode tasks" subtitle="Newest tenant-scoped records · polling interval 10 seconds" action={<button className="rc-text-btn" onClick={() => setPage("tasks")}>Open task queue <ArrowRight size={14} /></button>}>
                  <TaskTable tasks={tasks.slice(0, 6)} onOpen={openTask} />
                </Panel>
                <Panel title="RUSA AI product pillars" subtitle="Integration status is based on source/API evidence">
                  <div className="rc-pillar-grid">
                    {PILLARS.map((pillar) => <article className="rc-pillar-card" key={pillar.name}><div className="rc-pillar-top"><strong>{pillar.name}</strong><Badge tone={pillar.status === "Partial integration" ? "warning" : "neutral"}>{pillar.status}</Badge></div><p>{pillar.detail}</p></article>)}
                  </div>
                </Panel>
              </div>
            )}

            {page === "orchestrator" && (
              <div className="rc-stack">
                <Panel title="AICode execution pipeline" subtitle="Observed in rusa_agents/orchestrator.py · this is not the proposed /runs workflow API">
                  <div className="rc-pipeline">
                    {["Analyst", "Architect", "Developer", "Debugger"].map((name, index) => <div className="rc-pipeline-node" key={name}><span>{String(index + 1).padStart(2, "0")}</span><strong>{name}</strong><small>{index === 0 ? "Repository analysis" : index === 1 ? "Technical design" : index === 2 ? "Implementation" : "Repair role"}</small></div>)}
                    <div className="rc-pipeline-branch"><ArrowDownRight size={16} /><span>Quality gate</span></div>
                    <div className="rc-pipeline-parallel"><div className="rc-pipeline-node"><span>05A</span><strong>Tester</strong><small>Required test evidence</small></div><div className="rc-pipeline-node"><span>05B</span><strong>Security</strong><small>Required security evidence</small></div></div>
                  </div>
                  <div className="rc-inline-grid">
                    <div className="rc-inline-stat"><strong>4</strong><span>Primary roles in sequence</span></div>
                    <div className="rc-inline-stat"><strong>2</strong><span>Quality roles run in parallel</span></div>
                    <div className="rc-inline-stat"><strong>0–2</strong><span>Repair cycles configured by default</span></div>
                  </div>
                  <div className="rc-note">The orchestrator checks DAG dependencies, policy decisions, worker results, and required evidence. Missing/failed evidence can block the task or trigger a bounded repair cycle.</div>
                </Panel>
                <Panel title="Supreme Orchestrator workflow" subtitle="10-step responsibility sequence from RUSA_AI_All_Agents.md">
                  <div className="rc-step-grid">{ORCHESTRATOR_STEPS.map((step, index) => <div className="rc-step" key={step}><span>{String(index + 1).padStart(2, "0")}</span><strong>{step}</strong></div>)}</div>
                  <div className="rc-note">Architecture role Supreme Orchestrator and runtime class AICodeOrchestrator are shown separately; canonical identity mapping is not inferred.</div>
                </Panel>
                <Panel title="Inter-agent communication target" subtitle="ADR-COMM-001 · status: Diusulkan">
                  <div className="rc-comm-grid">
                    {[["RPC", "gRPC + Protobuf", "Short synchronous internal requests"], ["Command queue", "RabbitMQ or equivalent", "Durable targeted delegation"], ["Event log", "Kafka or equivalent", "Lifecycle, policy, and audit facts"], ["Gateway stream", "WebSocket + cursor", "Progress projection and reconnect"]].map(([name, protocol, detail]) => <div className="rc-comm-card" key={name}><strong>{name}</strong><Badge tone="warning">Proposed</Badge><span>{protocol}</span><small>{detail}</small></div>)}
                  </div>
                  <div className="rc-alert rc-alert-warning"><AlertTriangle size={16} /> ADR-COMM-001 masih berstatus Diusulkan. UI tidak menganggap RabbitMQ, Kafka, gRPC, /runs, outbox, atau WebSocket sudah aktif.</div>
                </Panel>
              </div>
            )}

            {page === "tasks" && (
              <div className="rc-stack">
                <Panel title="Tenant task queue" subtitle={taskTotal + " records · 25 per page"} action={<button className="rc-btn rc-btn-primary" onClick={() => setNewTaskOpen(true)} disabled={!canCreate}><Plus size={15} /> New task</button>}>
                  <div className="rc-toolbar">
                    <div className="rc-search"><Search size={15} /><input value={taskSearch} onChange={(event) => setTaskSearch(event.target.value)} placeholder="Search objective, task ID, repository…" /></div>
                    <select value={taskFilter} onChange={(event) => { const value = event.target.value as TaskFilter; setTaskFilter(value); setTaskOffset(0); void refreshTasks(0, value); }}>
                      <option value="all">All statuses</option><option value="active">Active (loaded page)</option><option value="queued">Queued</option><option value="running">Running</option><option value="awaiting_approval">Awaiting approval</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option>
                    </select>
                  </div>
                  <TaskTable tasks={visibleTasks} onOpen={openTask} />
                  <div className="rc-pagination"><span>Page {currentPage} of {pageCount} · {taskTotal} total records</span><div><button className="rc-btn rc-btn-ghost" disabled={taskOffset === 0} onClick={() => { const next = Math.max(0, taskOffset - 25); setTaskOffset(next); void refreshTasks(next, taskFilter); }}><ChevronLeft size={15} /> Previous</button><button className="rc-btn rc-btn-ghost" disabled={taskOffset + 25 >= taskTotal} onClick={() => { const next = taskOffset + 25; setTaskOffset(next); void refreshTasks(next, taskFilter); }}>Next <ChevronRight size={15} /></button></div></div>
                  <div className="rc-note">Status filter is applied server-side. Search and Active filter apply only to the currently loaded page. The backend list filter does not expose a blocked status.</div>
                </Panel>
                <Panel title="Task submission contract" subtitle="POST /v1/aicode/tasks">
                  <div className="rc-contract-grid"><div><strong>Required</strong><span>objective · repository.full_name · acceptance_criteria (1–30 items)</span></div><div><strong>Optional</strong><span>context · repository.base_branch</span></div><div><strong>Limits</strong><span>Objective ≤ 8,000 chars · context ≤ 20,000 · criterion ≤ 1,000</span></div><div><strong>Response</strong><span>202 Accepted · task_id · status · tenant_id · work_branch_prefix</span></div></div>
                </Panel>
              </div>
            )}

            {page === "approvals" && (
              <Panel title="Human review queue" subtitle={approvalTasks.length + " awaiting_approval records loaded (maximum 100)"}>
                <div className="rc-alert rc-alert-warning"><ShieldAlert size={17} /> Backend menyediakan state awaiting_approval dan endpoint retry, tetapi belum menyediakan approve/reject endpoint atau approval record. Karena itu UI sengaja tidak menampilkan tombol approve/reject maupun retry dari halaman ini. Keputusan harus dilakukan melalui proses governance yang benar-benar tercatat.</div>
                {approvalTasks.length === 0 ? <EmptyState title="Tidak ada task menunggu review" detail="Tidak ditemukan record awaiting_approval pada 100 hasil pertama." /> : <div className="rc-approval-list">{approvalTasks.map((task) => <div className="rc-approval-row" key={task.task_id}><div className="rc-status-icon"><ShieldAlert size={17} /></div><div className="rc-grow"><strong>{taskObjective(task)}</strong><small>{task.task_id} · {task.payload?.repository?.full_name || "Repository not provided"}</small><small>Updated {formatDate(task.updated_at)} · attempt {task.attempts}/{task.max_attempts}</small></div><Badge tone="warning">Awaiting approval</Badge><button className="rc-btn rc-btn-ghost" onClick={() => void openTask(task.task_id)}>Review evidence <ArrowRight size={14} /></button></div>)}</div>}
              </Panel>
            )}

            {page === "agents" && (
              <div className="rc-stack">
                <Panel title="Current AICode runtime components" subtitle="Source-defined inventory from WorkerRole, AgentManager, and AICodeOrchestrator">
                  <div className="rc-agent-grid">{RUNTIME_COMPONENTS.map((agent) => <article className="rc-agent-card" key={agent.name}><div className="rc-agent-card-top"><div className="rc-agent-icon"><Bot size={17} /></div><Badge tone="info">Code-defined</Badge></div><strong>{agent.name}</strong><span>{agent.type}</span><small>{agent.manager}</small><p>{agent.detail}</p></article>)}</div>
                  <div className="rc-note">This repository does not expose a live agent registry, heartbeat, per-agent health, or runtime instance endpoint. “Code-defined” is not equivalent to “currently online”.</div>
                </Panel>
                <Panel title="10 architecture agent roles" subtitle="Click a role to inspect objective, responsibilities, inputs, outputs, and guardrails from RUSA_AI_All_Agents.md">
                  <div className="rc-agent-grid">{ARCHITECTURE_AGENTS.map((agent, index) => <button className="rc-agent-card rc-agent-button" key={agent.name} onClick={() => setActiveAgent(agent)}><div className="rc-agent-card-top"><div className="rc-agent-icon"><BrainCircuit size={17} /></div><Badge tone={agent.priority === "Critical" ? "danger" : "neutral"}>{agent.priority}</Badge></div><strong>{String(index + 1).padStart(2, "0")} · {agent.name}</strong><p>{agent.objective}</p><span className="rc-text-btn">View role contract <ArrowRight size={13} /></span></button>)}</div>
                </Panel>
              </div>
            )}

            {page === "devops" && (
              <div className="rc-stack">
                <Panel title="Eight-stage Agentic DevOps lifecycle" subtitle="Stage definitions from RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE.md">
                  <div className="rc-lifecycle-layout">
                    <div className="rc-stage-list">{DEVOPS_STAGES.map((stage, index) => <button className={"rc-stage-item" + (activeStage?.id === stage.id ? " active" : "")} key={stage.id} onClick={() => setActiveStage(stage)}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{stage.name}</strong><small>{stage.id}</small></div><Badge tone="neutral">Spec</Badge></button>)}</div>
                    {activeStage && <div className="rc-stage-detail"><div className="rc-eyebrow">{activeStage.id}</div><h2>{activeStage.name} Agent</h2><p>{activeStage.purpose}</p><div className="rc-detail-columns"><div><h3>Inputs</h3><ul>{activeStage.inputs.map((item) => <li key={item}>{item}</li>)}</ul></div><div><h3>Outputs</h3><ul>{activeStage.outputs.map((item) => <li key={item}>{item}</li>)}</ul></div></div><h3>Exit criteria</h3><ul>{activeStage.exit.map((item) => <li key={item}>{item}</li>)}</ul><div className="rc-alert rc-alert-warning"><LockKeyhole size={15} /> Authority boundary: {activeStage.boundary}</div></div>}
                  </div>
                </Panel>
                <div className="rc-alert rc-alert-warning"><AlertTriangle size={16} /> Delapan stage ini adalah spesifikasi lifecycle. Backend AICode saat ini menjalankan worker roles Analyst, Architect, Developer, Debugger, Tester, dan Security; belum ada lifecycle registry/API untuk mengklaim delapan stage tersebut aktif.</div>
              </div>
            )}

            {page === "chat" && (
              <Panel title="Inference workspace" subtitle="POST /v1/chat/completions · streaming requested when enabled">
                {!canUseChat && <div className="rc-alert rc-alert-warning">Role read_only tidak memiliki permission inference pada backend.</div>}
                <div className="rc-chat-toolbar"><label>Model<select value={chatModel} onChange={(event) => setChatModel(event.target.value)} disabled={!models.length}><option value="">{models.length ? "Select model" : "Model discovery unavailable"}</option>{models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label><label className="rc-toggle"><input type="checkbox" checked={chatStream} onChange={(event) => setChatStream(event.target.checked)} /> Request streaming</label><Badge tone={models.length ? "success" : "warning"}>{models.length ? models.length + " model(s)" : "Model unknown"}</Badge></div>
                {modelError && <div className="rc-note">Model discovery: {modelError}. Chat can use the backend-configured default model.</div>}
                <div className="rc-chat-history">{chatMessages.map((message, index) => <div className={"rc-chat-line " + message.role} key={index}><div className="rc-chat-avatar">{message.role === "assistant" ? <BrainCircuit size={16} /> : "U"}</div><div className="rc-chat-bubble"><small>{message.role === "assistant" ? "RUSA AI" : "YOU"}</small><p>{message.content || (chatBusy ? "RUSA AI sedang menulis…" : "")}</p></div></div>)}{chatBusy && <div className="rc-chat-working"><span className="rc-live-dot" /> Inference berjalan…</div>}</div>
                <form className="rc-chat-compose" onSubmit={sendChat}><textarea value={chatInput} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={canUseChat ? "Tulis pesan untuk RUSA AI…" : "Inference tidak tersedia untuk role ini"} disabled={!canUseChat || chatBusy} /><button className="rc-btn rc-btn-primary" type="submit" disabled={!canUseChat || chatBusy || !chatInput.trim()}><Send size={16} /> Send</button></form>
                <div className="rc-note">Model selector berasal dari GET /v1/models. Jika provider tidak mendukung stream (HTTP 501), UI otomatis mencoba non-streaming. Chat tidak menjalankan AICode task.</div>
              </Panel>
            )}

            {page === "knowledge" && (
              <div className="rc-stack">
                <Panel title="Memory taxonomy" subtitle="Five categories defined in Memory & Knowledge Agent specification">
                  <div className="rc-memory-grid">{MEMORY_TYPES.map((item) => <article className="rc-memory-card" key={item.name}><div className="rc-agent-icon"><Database size={17} /></div><strong>{item.name}</strong><p>{item.detail}</p><small>{item.purpose}</small><Badge tone="neutral">Architecture contract</Badge></article>)}</div>
                </Panel>
                <div className="rc-alert rc-alert-warning"><AlertTriangle size={16} /> Tidak ditemukan endpoint web/backend untuk memory CRUD, semantic retrieval, indexing, retention, atau source management pada API yang diperiksa. UI ini menampilkan taxonomy dan batas implementasi; tidak membuat data memori palsu.</div>
              </div>
            )}

            {page === "repository" && (
              <div className="rc-stack">
                <Panel title="Artifact references" subtitle={"References extracted from " + loadedCount + " loaded task records"}>
                  {artifacts.length ? <div className="rc-artifact-list">{artifacts.map(({ task, artifact }, index) => <div className="rc-artifact-row" key={task.task_id + artifact + index}><div className="rc-agent-icon"><Package size={17} /></div><div className="rc-grow"><strong>{artifact}</strong><small>{taskObjective(task)} · {task.task_id}</small></div><button className="rc-btn rc-btn-ghost" onClick={() => void openTask(task.task_id)}>Task details</button></div>)}</div> : <EmptyState title="No artifact references in this page" detail="Task result belum mengembalikan array artifacts pada 25 record yang sedang dimuat." />}
                </Panel>
                <Panel title="Evidence keys" subtitle="Evidence values are displayed as references/data, not represented as verified file downloads">
                  {evidenceItems.length ? <div className="rc-evidence-list">{evidenceItems.map((item, index) => <div className="rc-evidence-row" key={item.task.task_id + item.key + index}><div className="rc-grow"><strong>{item.key}</strong><small>{taskObjective(item.task)} · {item.task.task_id}</small><code>{typeof item.value === "string" ? item.value : jsonText(item.value)}</code></div></div>)}</div> : <EmptyState title="No evidence keys in this page" detail="Evidence akan muncul jika worker mengembalikannya pada task result." />}
                </Panel>
                <div className="rc-alert rc-alert-warning"><Archive size={16} /> RUSA Repository pada dokumen lifecycle adalah evidence/artifact layer. Dedicated browse, download, provenance verification, and digest lookup API belum tersedia pada backend web yang diperiksa.</div>
              </div>
            )}

            {page === "evaluation" && (
              <div className="rc-stack">
                <div className="rc-kpi-grid">{KPI_GROUPS.map((group) => <Panel key={group.name} title={group.name} subtitle="Metric definitions from agent specification"><ul className="rc-kpi-list">{group.items.map((item) => <li key={item}><span>{item}</span><Badge tone="neutral">Defined</Badge></li>)}</ul></Panel>)}</div>
                <div className="rc-alert rc-alert-warning"><Gauge size={16} /> Evaluation & Reporting API, scorecard storage, historical trend, cost attribution, and evaluation jobs are not exposed by the current web API. No KPI values are fabricated.</div>
              </div>
            )}

            {page === "governance" && (
              <div className="rc-stack">
                <div className="rc-grid-two">
                  <Panel title="Authenticated principal" subtitle="Resolved from encrypted session via RUSA API">
                    <div className="rc-identity-list"><div><span>Subject</span><strong>{session.subject}</strong></div><div><span>Tenant ID</span><strong>{session.tenant_id}</strong></div><div><span>Roles</span><strong>{session.roles.join(", ")}</strong></div></div>
                  </Panel>
                  <Panel title="Role permission matrix" subtitle="TASK_ROLE_PERMISSIONS and INFERENCE_ALLOWED_ROLES in backend source">
                    <div className="rc-permission-list">{Object.entries(ROLE_PERMISSIONS).map(([role, permissions]) => <div key={role}><strong>{role}</strong><span>{permissions.join(" · ")}</span></div>)}</div>
                  </Panel>
                </div>
                <Panel title="Default autonomy policy boundaries" subtitle="Source-defined AutonomyPolicy defaults; deployment overrides are not queried by this UI">
                  <div className="rc-policy-grid"><div><h3>Protected · approval required</h3>{PROTECTED_ACTIONS.map((action) => <div className="rc-policy-item" key={action}><ShieldAlert size={14} /><code>{action}</code></div>)}</div><div><h3>Explicitly denied</h3>{DENIED_ACTIONS.map((action) => <div className="rc-policy-item danger" key={action}><LockKeyhole size={14} /><code>{action}</code></div>)}</div></div>
                  <div className="rc-note">Backend permission checks remain authoritative. Hiding a button in UI is not an authorization boundary; every mutation is revalidated server-side and audited.</div>
                </Panel>
              </div>
            )}

            {page === "observability" && (
              <div className="rc-stack">
                <div className="rc-stat-grid rc-stat-grid-2"><StatCard label="Liveness" value={livenessOk ? "Healthy" : "Unavailable"} detail="GET /health" icon={Activity} tone={livenessOk ? "green" : "red"} /><StatCard label="Readiness" value={readinessOk ? "Ready" : "Not ready"} detail="GET /health/ready" icon={Server} tone={readinessOk ? "green" : "red"} /></div>
                <div className="rc-grid-two">
                  <Panel title="Health response" subtitle="Backend readiness details">
                    <pre className="rc-code">{jsonText(health || { status: "not_loaded" })}</pre>
                  </Panel>
                  <Panel title="Metrics endpoint" subtitle="Operator-only GET /metrics · platform_admin role">
                    {!isPlatformAdmin ? <EmptyState title="Operator permission required" detail="Metrics endpoint hanya tersedia untuk platform_admin." /> : metricsError ? <div className="rc-alert rc-alert-warning">{metricsError}</div> : <><div className="rc-metrics-meta"><Badge tone={metricsText ? "success" : "warning"}>{metricsText ? "Fetched" : "Not fetched"}</Badge><small>{metricsFetchedAt ? formatDate(metricsFetchedAt) : "No fetch timestamp"}</small></div><MetricPreview text={metricsText} /><button className="rc-btn rc-btn-ghost" onClick={() => void refreshMetrics()}><RefreshCw size={14} /> Refresh metrics</button></>}
                  </Panel>
                </div>
                <Panel title="Telemetry delivery status" subtitle="Distinguish current behavior from ADR target">
                  <div className="rc-delivery-grid"><div><Badge tone="success">Current</Badge><strong>HTTP polling</strong><span>Task list: 10 seconds · Health: 30 seconds</span></div><div><Badge tone="warning">Proposed</Badge><strong>WebSocket + cursor</strong><span>ADR-COMM-001 describes replay/reconnect; no web socket endpoint is present in current app.</span></div><div><Badge tone="warning">Proposed</Badge><strong>Event log</strong><span>Kafka/outbox projection is an ADR target, not a verified deployment fact.</span></div></div>
                </Panel>
              </div>
            )}

            {page === "pillars" && (
              <div className="rc-pillar-detail-grid">{PILLARS.map((pillar, index) => <article className="rc-pillar-detail" key={pillar.name}><div className="rc-pillar-number">0{index + 1}</div><Badge tone={pillar.status === "Partial integration" ? "warning" : "neutral"}>{pillar.status}</Badge><h2>{pillar.name}</h2><p>{pillar.detail}</p><div className="rc-note">Evidence: {pillar.evidence}</div></article>)}</div>
            )}

            {page === "developer" && (
              <div className="rc-stack">
                <Panel title="Model discovery" subtitle="GET /v1/models">
                  {models.length ? <div className="rc-model-list">{models.map((model) => <div key={model}><Sparkles size={16} /><strong>{model}</strong><Badge tone="success">API returned</Badge></div>)}</div> : <div className="rc-alert rc-alert-warning">{modelError || "No model IDs returned by RUSA API."}</div>}
                </Panel>
                <Panel title="A2A task envelope validator" subtitle="POST /v1/validate-task · JSON Schema Draft 2020-12">
                  <form onSubmit={validateEnvelope} className="rc-validator">
                    <label>Envelope JSON<textarea spellCheck={false} value={envelopeText} onChange={(event) => { setEnvelopeText(event.target.value); setValidation(null); }} /></label>
                    <div className="rc-toolbar"><button className="rc-btn rc-btn-primary" type="submit" disabled={validationBusy}>{validationBusy ? "Validating…" : "Validate envelope"} <ArrowRight size={14} /></button><span className="rc-note">Endpoint validates the A2A envelope schema, not AICode task request fields.</span></div>
                  </form>
                  {validationError && <div className="rc-alert rc-alert-danger">{validationError}</div>}
                  {validation && <div className={"rc-validation-result " + (validation.valid ? "valid" : "invalid")}><div><Badge tone={validation.valid ? "success" : "danger"}>{validation.valid ? "VALID" : "INVALID"}</Badge><strong>{validation.valid ? "Envelope accepted by schema validator" : "Schema validation errors"}</strong></div>{validation.errors.length > 0 && <ul>{validation.errors.map((item, index) => <li key={index}>{item}</li>)}</ul>}</div>}
                </Panel>
              </div>
            )}
          </div>
        </section>
      </div>

      {newTaskOpen && (
        <div className="rc-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setNewTaskOpen(false); }}>
          <section className="rc-modal rc-modal-wide" role="dialog" aria-modal="true" aria-labelledby="rc-new-task-title">
            <div className="rc-modal-head"><div><div className="rc-eyebrow">AICODE TASK INTAKE</div><h2 id="rc-new-task-title">Create engineering task</h2><p>Input mengikuti kontrak POST /v1/aicode/tasks.</p></div><button className="rc-icon-btn" onClick={() => setNewTaskOpen(false)} aria-label="Close"><X size={18} /></button></div>
            <form className="rc-form rc-form-grid" onSubmit={createTask}>
              <label className="rc-field-full">Objective <span>Required · max 8,000 characters</span><textarea value={objective} onChange={(event) => setObjective(event.target.value)} maxLength={8000} rows={3} required placeholder="Jelaskan perubahan yang harus dikerjakan…" /></label>
              <label>Repository <span>owner/name format</span><input value={repository} onChange={(event) => setRepository(event.target.value)} required /></label>
              <label>Base branch <span>Default: main</span><input value={baseBranch} onChange={(event) => setBaseBranch(event.target.value)} required /></label>
              <label className="rc-field-full">Context <span>Optional · max 20,000 characters</span><textarea value={context} onChange={(event) => setContext(event.target.value)} maxLength={20000} rows={3} placeholder="Constraints, files, references, and technical context…" /></label>
              <label className="rc-field-full">Acceptance criteria <span>1–30 non-empty lines · max 1,000 characters each</span><textarea value={criteria} onChange={(event) => setCriteria(event.target.value)} rows={4} required /></label>
              <div className="rc-modal-actions rc-field-full"><button type="button" className="rc-btn rc-btn-ghost" onClick={() => setNewTaskOpen(false)}>Cancel</button><button type="submit" className="rc-btn rc-btn-primary" disabled={busy || !canCreate}>{busy ? "Submitting…" : "Submit task"} <ArrowRight size={15} /></button></div>
            </form>
          </section>
        </div>
      )}

      {selectedTask && (
        <div className="rc-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelectedTask(null); }}>
          <section className="rc-modal rc-task-modal" role="dialog" aria-modal="true" aria-labelledby="rc-task-title">
            <div className="rc-modal-head"><div><div className="rc-eyebrow">TASK DETAIL</div><h2 id="rc-task-title">{taskObjective(selectedTask)}</h2><small className="rc-mono">{selectedTask.task_id}</small></div><button className="rc-icon-btn" onClick={() => setSelectedTask(null)} aria-label="Close"><X size={18} /></button></div>
            <div className="rc-task-detail-grid"><div><span>Status</span><Badge tone={statusTone(selectedTask.status)}>{statusLabel(selectedTask.status)}</Badge></div><div><span>Repository</span><strong>{selectedTask.payload?.repository?.full_name || "—"}</strong></div><div><span>Attempts</span><strong>{selectedTask.attempts} / {selectedTask.max_attempts}</strong></div><div><span>Tenant</span><strong>{selectedTask.tenant_id}</strong></div><div><span>Created</span><strong>{formatDate(selectedTask.created_at)}</strong></div><div><span>Updated</span><strong>{formatDate(selectedTask.updated_at)}</strong></div></div>
            {selectedTask.payload?.context && <div className="rc-detail-section"><h3>Context</h3><p>{selectedTask.payload.context}</p></div>}
            {selectedTask.payload?.acceptance_criteria && <div className="rc-detail-section"><h3>Acceptance criteria</h3><ul>{selectedTask.payload.acceptance_criteria.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
            {selectedTask.last_error && <div className="rc-alert rc-alert-danger"><AlertTriangle size={15} /> {selectedTask.last_error}</div>}
            <div className="rc-detail-section"><h3>Result payload and worker evidence</h3><pre className="rc-code">{jsonText(selectedTask.result || { status: "No result payload yet" })}</pre></div>
            <div className="rc-modal-actions"><div className="rc-note">Actions are shown only when supported by current role and task state.</div>{canControl && selectedTask.status === "failed" && <button className="rc-btn rc-btn-ghost" disabled={busy} onClick={() => void retryTask(selectedTask.task_id)}><RefreshCw size={14} /> Retry failed task</button>}{canControl && ["queued", "running", "awaiting_approval"].includes(selectedTask.status) && <button className="rc-btn rc-btn-danger" disabled={busy} onClick={() => void cancelTask(selectedTask.task_id)}>Cancel task</button>}</div>
          </section>
        </div>
      )}

      {activeAgent && (
        <div className="rc-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setActiveAgent(null); }}>
          <section className="rc-modal rc-agent-modal" role="dialog" aria-modal="true" aria-labelledby="rc-agent-title">
            <div className="rc-modal-head"><div><div className="rc-eyebrow">ARCHITECTURE ROLE CONTRACT</div><h2 id="rc-agent-title">{activeAgent.name}</h2><p>{activeAgent.objective}</p></div><button className="rc-icon-btn" onClick={() => setActiveAgent(null)} aria-label="Close"><X size={18} /></button></div>
            <div className="rc-agent-contract-grid">
              <div><h3>Core responsibilities</h3><ul>{activeAgent.responsibilities.map((item) => <li key={item}>{item}</li>)}</ul></div>
              <div><h3>Inputs</h3><ul>{activeAgent.inputs.map((item) => <li key={item}>{item}</li>)}</ul></div>
              <div><h3>Outputs</h3><ul>{activeAgent.outputs.map((item) => <li key={item}>{item}</li>)}</ul></div>
              <div><h3>Authority boundaries</h3><ul>{activeAgent.guardrails.map((item) => <li key={item}>{item}</li>)}</ul>{activeAgent.extra && <p className="rc-extra-note">{activeAgent.extra}</p>}</div>
            </div>
            <div className="rc-alert rc-alert-warning"><Bot size={15} /> Architecture role contract. Runtime registration and live health are not exposed by the current API.</div>
          </section>
        </div>
      )}
    </main>
  );
}

function TaskTable({ tasks, onOpen }: { tasks: Task[]; onOpen: (taskId: string) => void }) {
  if (!tasks.length) return <EmptyState title="No task records" detail="Belum ada task pada filter atau halaman yang dipilih." />;
  return (
    <div className="rc-table-wrap"><table className="rc-table"><thead><tr><th>Task</th><th>Repository</th><th>Status</th><th>Attempts</th><th>Updated</th></tr></thead><tbody>{tasks.map((task) => <tr key={task.task_id}><td><button className="rc-table-link" onClick={() => onOpen(task.task_id)}>{taskObjective(task)}</button><small className="rc-mono">{task.task_id}</small></td><td>{task.payload?.repository?.full_name || "—"}</td><td><Badge tone={statusTone(task.status)}>{statusLabel(task.status)}</Badge></td><td>{task.attempts} / {task.max_attempts}</td><td>{formatDate(task.updated_at)}</td></tr>)}</tbody></table></div>
  );
}
