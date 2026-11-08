"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  orchestrator: { title: "Orchestrator", eyebrow: "WORKFLOW CONTROL", description: "Bandingkan pipeline AICode aktual, kontrak komunikasi All Agents, schema A2A repository, dan ADR komunikasi yang masih diusulkan." },
  tasks: { title: "AICode Tasks", eyebrow: "EXECUTION WORKSPACE", description: "Kelola task coding tenant-scoped melalui endpoint AICode yang tersedia." },
  approvals: { title: "Approval Center", eyebrow: "HUMAN GOVERNANCE", description: "Pantau task yang menunggu review manusia tanpa mengarang aksi approve atau reject yang belum tersedia." },
  agents: { title: "Agent Workspace", eyebrow: "AGENT INTELLIGENCE", description: "Lihat 7 komponen runtime yang terdefinisi di kode dan 10 peran agent dalam dokumen arsitektur." },
  devops: { title: "Agentic DevOps", eyebrow: "LIFECYCLE WORKSPACE", description: "Delapan tahap lifecycle sesuai dokumen DevOps, dengan status implementasi yang dibedakan secara eksplisit." },
  chat: { title: "RUSA AI Chat", eyebrow: "MODEL INTERFACE", description: "Inference melalui RUSA API dengan model discovery dan streaming bila provider mendukungnya." },
  knowledge: { title: "Memory & Knowledge", eyebrow: "KNOWLEDGE PLANE", description: "Pisahkan task memory yang aktif, retrieval RAG/MCP opsional, dan lima kategori memori pada kontrak arsitektur." },
  repository: { title: "Repository & Artifacts", eyebrow: "EVIDENCE LAYER", description: "Tampilkan referensi hasil task yang benar-benar dikembalikan API; bukti worker terperinci belum tersedia melalui task detail." },
  evaluation: { title: "Evaluation & Reporting", eyebrow: "QUALITY INTELLIGENCE", description: "Bedakan KPI agent yang masih berupa kontrak, retrieval evaluation offline, dan telemetry operasional Prometheus." },
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
  { name: "AI Autonomous Software Engineering & DevOps", status: "Partial integration", detail: "AICode task intake, orchestrator, worker pipeline, task state, retry, cancellation, dan task-result references (PR URL, commit SHA, branch).", evidence: "FastAPI /v1/aicode/tasks dan rusa_agents runtime." },
  { name: "RUSA Intelligence", status: "Partial integration", detail: "Inference API, model discovery, AICode runtime, tenant-scoped task memory, serta optional RAG/pgvector dan read-only MCP tersedia pada lapisan berbeda. Knowledge browser dan agent scorecard belum diekspos pada web API.", evidence: "FastAPI /v1/chat/completions dan /v1/models; rusa_agents memory; optional rusa_knowledge/rusa_mcp." },
  { name: "RUSA Business", status: "Architecture defined", detail: "Business operations automation, research, dan company analysis belum memiliki UI/API pada scope repository yang diperiksa.", evidence: "RUSA_AI_Architecture.md." },
  { name: "RUSA Ecosystem", status: "Architecture defined", detail: "Marketplace, developer API, external integrations, dan agent ecosystem belum memiliki UI/API pada scope repository yang diperiksa.", evidence: "RUSA_AI_Architecture.md." }
];

const ORCHESTRATOR_STEPS = ["RECEIVE", "UNDERSTAND", "PLAN", "DECOMPOSE", "DELEGATE", "EXECUTE", "VALIDATE", "REVIEW", "COMPLETE", "LEARN"];
const TASK_ROLE_PERMISSIONS: Record<string, string[]> = {
  platform_admin: ["task:create", "task:read", "task:retry", "task:cancel"],
  tenant_admin: ["task:create", "task:read", "task:retry", "task:cancel"],
  developer: ["task:create", "task:read"],
  read_only: ["task:read"]
};
const INFERENCE_ALLOWED_ROLES = ["platform_admin", "tenant_admin", "developer"];
const METRICS_ALLOWED_ROLE = "platform_admin";
const ALL_AGENT_TASK_STATUSES = ["PENDING", "READY", "RUNNING", "BLOCKED", "NEEDS_APPROVAL", "REVIEW", "COMPLETED", "FAILED", "CANCELLED"];
const DEVOPS_TASK_STATUSES = ["queued", "running", "passed", "failed", "blocked", "needs_approval", "cancelled", "skipped"];
const AICODE_API_STATUSES = ["queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled"];
const CURRENT_A2A_MESSAGE_TYPES = ["task.create", "task.accepted", "task.status", "task.result", "task.blocked", "task.review", "task.cancel"];
const ADR_A2A_MESSAGE_TYPES = ["task.request", "task.accepted", "task.progress", "tool.invocation", "task.completed", "task.failed", "policy.violation", "task.cancel.requested", "task.cancelled", "audit.recorded"];
const PROTECTED_ACTIONS = ["merge", "deploy", "release", "secret_change", "permission_change", "destructive_action", "production_write"];
const DENIED_ACTIONS = ["disable_audit", "exfiltrate_secret", "force_push_main"];

const DEFAULT_ENVELOPE = JSON.stringify({
  protocol_version: "1.0",
  task_id: "550e8400-e29b-41d4-a716-446655440000",
  message_id: "550e8400-e29b-41d4-a716-446655440001",
  type: "task.create",
  from: { agent_id: "workspace-user", role: "human" },
  to: { agent_id: "rusa-orchestrator", role: "rusa_orchestrator" },
  created_at: "2026-11-06T11:00:00Z",
  task: {
    objective: "Implement the requested RUSA AI change",
    context: "Validate the A2A envelope against the repository schema.",
    acceptance_criteria: [
      "The change follows the approved architecture",
      "Relevant tests pass"
    ],
    repository: "StrategyG/RusaAi",
    base_branch: "main",
    work_branch: "feature/ui-ux-precision"
  },
  policy: {
    allowed_actions: ["repo_read", "branch_create", "workspace_write", "test_run"],
    prohibited_actions: ["force_push_main", "exfiltrate_secret"],
    human_approval_required: ["merge", "deploy", "release", "permission_change"]
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
type TaskReference = { kind: string; value: string; href?: string };
function extractTaskReferences(task: Task): TaskReference[] {
  const result = resultObject(task);
  const references: TaskReference[] = [];
  const repository = task.payload?.repository?.full_name || "";
  const pullRequestUrl = result.pull_request_url;
  if (typeof pullRequestUrl === "string") {
    try {
      const url = new URL(pullRequestUrl);
      if (url.protocol === "https:" && url.hostname === "github.com" && !url.username && !url.password && !url.port && /^\/[^/]+\/[^/]+\/pull\/\d+(?:\/|$)/.test(url.pathname)) {
        references.push({ kind: "Pull request", value: url.toString(), href: url.toString() });
      }
    } catch { /* Ignore untrusted or malformed result references. */ }
  }
  const commitSha = result.commit_sha;
  if (typeof commitSha === "string" && /^[a-fA-F0-9]{7,64}$/.test(commitSha)) {
    const repoParts = repository.split("/");
    const href = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository) && repoParts.every((part) => part !== "." && part !== "..")
      ? "https://github.com/" + repository + "/commit/" + commitSha
      : undefined;
    references.push({ kind: "Commit", value: commitSha, href });
  }
  const branch = result.branch;
  if (typeof branch === "string" && branch.length > 0 && branch.length <= 255) {
    references.push({ kind: "Feature branch", value: branch });
  }
  const artifacts = result.artifacts;
  if (Array.isArray(artifacts)) {
    for (const artifact of artifacts) {
      if (typeof artifact === "string" && artifact.length > 0 && artifact.length <= 2048) {
        references.push({ kind: "Artifact reference", value: artifact });
      }
    }
  }
  return references;
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
  const [allTaskTotal, setAllTaskTotal] = useState(0);
  const [taskOffset, setTaskOffset] = useState(0);
  const [taskFilter, setTaskFilter] = useState<TaskFilter>("all");
  const [taskSearch, setTaskSearch] = useState("");
  const [approvalTasks, setApprovalTasks] = useState<Task[]>([]);
  const [approvalTotal, setApprovalTotal] = useState(0);
  const [approvalFetchedAt, setApprovalFetchedAt] = useState("");
  const [approvalError, setApprovalError] = useState("");
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const taskIdempotencyRef = useRef<{ fingerprint: string; key: string } | null>(null);
  const taskRequestSeqRef = useRef(0);
  const approvalRequestSeqRef = useRef(0);
  const totalRequestSeqRef = useRef(0);
  const healthRequestSeqRef = useRef(0);
  const modelRequestSeqRef = useRef(0);
  const metricsRequestSeqRef = useRef(0);

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
    const requestSeq = ++taskRequestSeqRef.current;
    const params = new URLSearchParams({ limit: "25", offset: String(offset) });
    if (filter !== "all" && filter !== "active") params.set("status", filter);
    try {
      const response = await fetch("/api/tasks?" + params.toString(), { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      if (requestSeq !== taskRequestSeqRef.current) return;
      const result = data as TaskPage;
      setTasks(Array.isArray(result.items) ? result.items : []);
      setTaskTotal(typeof result.total === "number" ? result.total : 0);
      if (filter === "all") setAllTaskTotal(typeof result.total === "number" ? result.total : 0);
      setError("");
    } catch (cause) {
      if (requestSeq === taskRequestSeqRef.current) setError(cause instanceof Error ? cause.message : "Could not refresh task queue.");
    }
  }, []);

  const refreshApprovals = useCallback(async () => {
    const requestSeq = ++approvalRequestSeqRef.current;
    try {
      const response = await fetch("/api/tasks?status=awaiting_approval&limit=100&offset=0", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      if (requestSeq !== approvalRequestSeqRef.current) return;
      const items = Array.isArray(data.items) ? data.items as Task[] : [];
      setApprovalTasks(items);
      setApprovalTotal(typeof data.total === "number" ? data.total : items.length);
      setApprovalFetchedAt(new Date().toISOString());
      setApprovalError("");
    } catch (cause) {
      if (requestSeq === approvalRequestSeqRef.current) setApprovalError(cause instanceof Error ? cause.message : "Approval queue refresh failed.");
      // Keep the last successful snapshot, but label it as stale in the UI.
    }
  }, []);

  const refreshTenantTotal = useCallback(async () => {
    const requestSeq = ++totalRequestSeqRef.current;
    try {
      const response = await fetch("/api/tasks?limit=1&offset=0", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (requestSeq === totalRequestSeqRef.current && response.ok && typeof data.total === "number") setAllTaskTotal(data.total);
    } catch {
      // Keep the last successful tenant total.
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    const requestSeq = ++healthRequestSeqRef.current;
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const data = response.ok ? await response.json() : null;
      if (requestSeq === healthRequestSeqRef.current) setHealth(data);
    } catch {
      if (requestSeq === healthRequestSeqRef.current) setHealth(null);
    }
  }, []);

  const refreshModels = useCallback(async () => {
    const requestSeq = ++modelRequestSeqRef.current;
    try {
      const response = await fetch("/api/models", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      if (requestSeq !== modelRequestSeqRef.current) return;
      const ids = Array.isArray(data.data) ? data.data.map((item: { id?: unknown }) => item.id).filter((id: unknown): id is string => typeof id === "string") : [];
      setModels(ids);
      setChatModel((current) => ids.includes(current) ? current : (ids[0] || ""));
      setModelError("");
    } catch (cause) {
      if (requestSeq === modelRequestSeqRef.current) {
        setModels([]);
        setModelError(cause instanceof Error ? cause.message : "Model discovery unavailable.");
      }
    }
  }, []);

  const refreshMetrics = useCallback(async () => {
    if (!isPlatformAdmin) return;
    const requestSeq = ++metricsRequestSeqRef.current;
    setMetricsError("");
    try {
      const response = await fetch("/api/metrics", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      if (requestSeq !== metricsRequestSeqRef.current) return;
      setMetricsText(typeof data.text === "string" ? data.text : "");
      setMetricsFetchedAt(typeof data.fetched_at === "string" ? data.fetched_at : "");
    } catch (cause) {
      if (requestSeq === metricsRequestSeqRef.current) {
        setMetricsError(cause instanceof Error ? cause.message : "Metrics endpoint unavailable.");
        setMetricsText("");
      }
    }
  }, [isPlatformAdmin]);

  const loadSession = useCallback(async () => {
    try {
      const response = await fetch("/api/session", { cache: "no-store" });
      if (response.ok) {
        const identity = await response.json();
        setSession(identity);
        await Promise.all([refreshTasks(0, "all"), refreshApprovals(), refreshHealth(), refreshModels()]);
      } else if (response.status !== 401) {
        const data = await response.json().catch(() => ({}));
        setLoginError(errorText(data));
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
    const totalTimer = window.setInterval(() => { void refreshTenantTotal(); }, 30000);
    return () => {
      window.clearInterval(taskTimer);
      window.clearInterval(healthTimer);
      window.clearInterval(totalTimer);
    };
  }, [session, taskOffset, taskFilter, refreshTasks, refreshApprovals, refreshHealth, refreshTenantTotal]);

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
    taskRequestSeqRef.current += 1;
    approvalRequestSeqRef.current += 1;
    totalRequestSeqRef.current += 1;
    healthRequestSeqRef.current += 1;
    modelRequestSeqRef.current += 1;
    metricsRequestSeqRef.current += 1;
    taskIdempotencyRef.current = null;
    setSession(null);
    setTasks([]);
    setApprovalTasks([]);
    setApprovalTotal(0);
    setApprovalFetchedAt("");
    setApprovalError("");
    setTaskTotal(0);
    setAllTaskTotal(0);
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
    const repositoryParts = repository.trim().split("/");
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository.trim()) || repositoryParts.some((part) => part === "." || part === "..")) {
      setError("Repository harus menggunakan format owner/name dengan segmen yang valid.");
      return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$/.test(baseBranch.trim()) || baseBranch.includes("..") || baseBranch.includes("//") || baseBranch.endsWith("/")) {
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
    const taskPayload = {
      objective: objective.trim(),
      context,
      repository: { full_name: repository.trim(), base_branch: baseBranch.trim() },
      acceptance_criteria: acceptanceCriteria
    };
    const fingerprint = JSON.stringify(taskPayload);
    if (!taskIdempotencyRef.current || taskIdempotencyRef.current.fingerprint !== fingerprint) {
      taskIdempotencyRef.current = { fingerprint, key: window.crypto.randomUUID() };
    }
    const idempotencyKey = taskIdempotencyRef.current.key;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
        body: JSON.stringify(taskPayload)
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      taskIdempotencyRef.current = null;
      setNewTaskOpen(false);
      setTaskFilter("all");
      setTaskOffset(0);
      setTaskSearch("");
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
    const nextMessages = [...chatMessages, { role: "user" as const, content: prompt }].slice(-32);
    const requestMessages = nextMessages.map((message) => ({ ...message, content: message.content.slice(-6000) }));
    while (requestMessages.length > 1 && new TextEncoder().encode(JSON.stringify(requestMessages)).byteLength > 900 * 1024) {
      requestMessages.shift();
    }
    setChatMessages(nextMessages);
    setChatInput("");
    setChatBusy(true);
    let placeholderAdded = false;
    const updateAssistant = (content: string) => {
      setChatMessages((current) => {
        const next = [...current];
        const index = next.length - 1;
        if (index >= 0 && next[index].role === "assistant") next[index] = { role: "assistant", content };
        return next.slice(-32);
      });
    };
    try {
      const body = {
        ...(chatModel ? { model: chatModel } : {}),
        messages: requestMessages.map((item) => ({ role: item.role, content: item.content })),
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
        setChatMessages((current) => [...current, { role: "assistant", content: "" }].slice(-32));
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
        setChatMessages((current) => [...current, { role: "assistant", content: answer }].slice(-32));
      }
    } catch (cause) {
      const message = "Request gagal: " + (cause instanceof Error ? cause.message : "Unknown error");
      if (placeholderAdded) updateAssistant(message);
      else setChatMessages((current) => [...current, { role: "assistant", content: message }].slice(-32));
    } finally { setChatBusy(false); }
  }

  async function validateEnvelope(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidation(null);
    setValidationError("");
    if (new TextEncoder().encode(envelopeText).byteLength > 262144) {
      setValidationError("Envelope melebihi batas 256 KiB untuk validator web.");
      return;
    }
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
  const taskReferences = tasks.flatMap((task) => extractTaskReferences(task).map((reference) => ({ task, reference })));

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
                      {item.id === "approvals" && approvalTotal > 0 && <b>{approvalTotal > 99 ? "99+" : approvalTotal}</b>}
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
            <div className="rc-topbar-right"><span className="rc-role-pill">{session.roles.join(" · ")}</span><button className="rc-btn rc-btn-ghost" onClick={() => { void refreshTasks(taskOffset, taskFilter); void refreshTenantTotal(); void refreshApprovals(); void refreshHealth(); if (page === "observability") void refreshMetrics(); }}><RefreshCw size={14} /> Refresh</button></div>
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
                  <StatCard label="Total task records" value={allTaskTotal} detail="All records in authenticated tenant" icon={ListChecks} />
                  <StatCard label="Active in loaded page" value={counts.active} detail={"From " + loadedCount + " loaded records"} icon={Play} tone="blue" />
                  <StatCard label="Awaiting approval" value={approvalTotal} detail={approvalError ? "Last successful snapshot · stale" : "Tenant total · " + approvalTasks.length + " loaded (max 100)"} icon={ShieldAlert} tone="amber" />
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
                <Panel title="Loaded AICode task sample" subtitle="Current loaded page and selected queue filter · polling interval 10 seconds" action={<button className="rc-text-btn" onClick={() => setPage("tasks")}>Open task queue <ArrowRight size={14} /></button>}>
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
                    {[["External API", "HTTPS + JSON", "POST /runs → 202; GET /runs/{id}; POST /runs/{id}/cancel idempotently"], ["RPC", "gRPC + Protobuf", "Short synchronous internal calls with explicit deadlines"], ["Command queue", "RabbitMQ or equivalent", "Durable targeted delegation, competing consumers, confirms, manual ACK, DLQ"], ["Event log", "Kafka or equivalent", "Lifecycle, policy, and audit facts via transactional outbox"], ["Gateway stream", "WebSocket + cursor", "Progress projection, reconnect, replay or snapshot"], ["Canonical state", "Workflow store + outbox", "Atomic run/state/outbox persistence; gateway serves a projection"]].map(([name, protocol, detail]) => <div className="rc-comm-card" key={name}><strong>{name}</strong><Badge tone="warning">Proposed</Badge><span>{protocol}</span><small>{detail}</small></div>)}
                  </div>
                  <div className="rc-alert rc-alert-warning"><AlertTriangle size={16} /> ADR-COMM-001 masih berstatus Diusulkan. RabbitMQ dan Kafka adalah baseline yang diusulkan; tidak ada bukti deployment aktif untuk gRPC, /runs, outbox, broker, event log, atau WebSocket pada API web saat ini.</div>
                </Panel>
                <Panel title="Three distinct agent communication contracts" subtitle="Do not silently map one schema or status vocabulary to another">
                  <div className="rc-contract-grid"><div><strong>All Agents · conceptual contract v1.0</strong><span>Request fields: task_id, parent_task_id, source_agent, target_agent, objective, context, constraints, allowed_tools, priority, approval_required, acceptance_criteria, deadline. Response fields: agent, status, summary, artifacts, evidence, risks, unresolved_issues, next_actions.</span></div><div><strong>Repository validator · executable schema</strong><span>schemas/a2a/task-envelope.schema.json uses protocol_version=1.0, from/to agent identities, task, policy, and current message types. POST /v1/validate-task validates only this schema.</span></div><div><strong>ADR-COMM-001 · proposed envelope</strong><span>schema_version=1, tenant_id, run_id, correlation_id, traceparent, producer, occurred_at, and payload; task_id/attempt are conditional by message type and causation_id is optional. It is not the schema used by the current validator.</span></div><div><strong>Current AICode task API</strong><span>Tenant task list states: {AICODE_API_STATUSES.join(", ")}. This API state model is separate from lifecycle-stage and conceptual agent status vocabularies.</span></div></div>
                  <div className="rc-detail-columns"><div><h3>All Agents conceptual statuses</h3><div className="rc-chip-row">{ALL_AGENT_TASK_STATUSES.map((status) => <Badge key={status} tone="neutral">{status}</Badge>)}</div></div><div><h3>Agentic DevOps stage statuses</h3><div className="rc-chip-row">{DEVOPS_TASK_STATUSES.map((status) => <Badge key={status} tone="neutral">{status}</Badge>)}</div></div></div>
                  <div className="rc-detail-section"><h3>Message type vocabularies</h3><p><strong>Current repository schema:</strong> {CURRENT_A2A_MESSAGE_TYPES.join(" · ")}</p><p><strong>ADR proposal:</strong> {ADR_A2A_MESSAGE_TYPES.join(" · ")}</p></div>
                </Panel>
                <Panel title="ADR delivery, reliability and security rules" subtitle="Proposed contract · not current API guarantees">
                  <div className="rc-contract-grid"><div><strong>At-least-once delivery</strong><span>Transport is at-least-once. Consumers deduplicate by tenant_id + message_id; duplicate delivery must not grant a second execution.</span></div><div><strong>Transactional outbox and ACK</strong><span>Run state and outbox commit atomically. Relay waits for broker confirmation before marking sent; consumer persists inbox/lease/accepted state before ACK.</span></div><div><strong>Ingress idempotency</strong><span>Scope key by tenant and endpoint, and store the request-body hash. Same key + same body returns the same run; same key + different body returns 409.</span></div><div><strong>Lease and ordering</strong><span>Lease epoch and task state version increase monotonically. Stale epochs and stale state updates are rejected; workers heartbeat their leases.</span></div><div><strong>Retry and deadline</strong><span>Up to two additional transient attempts with exponential backoff and full jitter. Transport redelivery keeps the same attempt; policy denial is not retried. Propagate absolute deadlines.</span></div><div><strong>Cancellation and side effects</strong><span>Cancel is a P0 control command. CANCEL_REQUESTED blocks new claims; ambiguous external effects go to reconciliation. Final result and cancel race through one state transaction.</span></div><div><strong>Service identity and signing</strong><span>Internal mTLS, short-lived audience-scoped service tokens, tenant/scope checks, and detached JWS signatures for durable commands plus audit/policy events. Never put bearer tokens in durable events.</span></div><div><strong>Acceptance targets</strong><span>Proposed initial targets: POST /runs 202 p95 ≤ 500 ms; dispatch-to-claim p95 ≤ 2 s; cancel observed by connected worker p95 ≤ 2 s; verify isolation, signatures, lease fencing, and same-digest QA.</span></div></div>
                  <div className="rc-alert rc-alert-warning"><ShieldAlert size={16} /> These reliability, lease, outbox, signing, and SLO requirements belong to ADR-COMM-001 and remain proposed. They are not supplied by the current AICode task API. Its two repair cycles are also distinct from the ADR's two additional transient transport/task attempts.</div>
                </Panel>
              </div>
            )}

            {page === "tasks" && (
              <div className="rc-stack">
                <Panel title="Tenant task queue" subtitle={taskTotal + " records · 25 per page"} action={<button className="rc-btn rc-btn-primary" onClick={() => setNewTaskOpen(true)} disabled={!canCreate}><Plus size={15} /> New task</button>}>
                  <div className="rc-toolbar">
                    <div className="rc-search"><Search size={15} /><input value={taskSearch} onChange={(event) => setTaskSearch(event.target.value)} placeholder="Search loaded page: objective, task ID, repository…" /></div>
                    <select value={taskFilter} onChange={(event) => { const value = event.target.value as TaskFilter; setTaskFilter(value); setTaskOffset(0); void refreshTasks(0, value); }}>
                      <option value="all">All statuses</option><option value="active">Active (loaded page)</option><option value="queued">Queued</option><option value="running">Running</option><option value="awaiting_approval">Awaiting approval</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option>
                    </select>
                  </div>
                  <div className="rc-note">Search and Active are client-side filters over the currently loaded 25-record page. Other status filters are server-side. Use pagination before concluding that a matching task does not exist.</div>
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
              <Panel title="Human review queue" subtitle={"Showing " + approvalTasks.length + " of " + approvalTotal + " awaiting_approval records · maximum 100 per refresh"}>
                <div className="rc-alert rc-alert-warning"><ShieldAlert size={17} /> Backend menyediakan state awaiting_approval dan endpoint retry, tetapi belum menyediakan approval decision endpoint atau approval record. Retry bukan approval. UI tidak menyediakan approve/reject atau retry dari antrean ini.</div>
                {approvalError && <div className="rc-alert rc-alert-danger"><AlertTriangle size={15} /> Refresh gagal: {approvalError}{approvalFetchedAt ? " · Snapshot terakhir berhasil " + formatDate(approvalFetchedAt) : " · Belum ada snapshot berhasil."}</div>}
                {approvalTasks.length === 0 ? <EmptyState title={approvalError ? "Approval queue unavailable" : "Tidak ada task menunggu review"} detail={approvalError || "Tidak ditemukan record awaiting_approval pada hasil yang dimuat."} /> : <div className="rc-approval-list">{approvalTasks.map((task) => <div className="rc-approval-row" key={task.task_id}><div className="rc-status-icon"><ShieldAlert size={17} /></div><div className="rc-grow"><strong>{taskObjective(task)}</strong><small>{task.task_id} · {task.payload?.repository?.full_name || "Repository not provided"}</small><small>Updated {formatDate(task.updated_at)} · attempt {task.attempts}/{task.max_attempts}</small></div><Badge tone="warning">Awaiting approval</Badge><button className="rc-btn rc-btn-ghost" onClick={() => void openTask(task.task_id)}>Inspect task <ArrowRight size={14} /></button></div>)}</div>}
                {approvalFetchedAt && !approvalError && <div className="rc-note">Last successful refresh: {formatDate(approvalFetchedAt)}. The API total is tenant-scoped; the UI loads at most 100 records for inspection.</div>}
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
                <Panel title="Eight-stage Agentic DevOps lifecycle" subtitle="Proposed Architecture v1.0 · stage definitions from RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE.md">
                  <div className="rc-lifecycle-layout">
                    <div className="rc-stage-list">{DEVOPS_STAGES.map((stage, index) => <button className={"rc-stage-item" + (activeStage?.id === stage.id ? " active" : "")} key={stage.id} onClick={() => setActiveStage(stage)}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{stage.name}</strong><small>{stage.id}</small></div><Badge tone="warning">Proposed</Badge></button>)}</div>
                    {activeStage && <div className="rc-stage-detail"><div className="rc-eyebrow">{activeStage.id}</div><h2>{activeStage.name} Agent</h2><p>{activeStage.purpose}</p><div className="rc-detail-columns"><div><h3>Inputs</h3><ul>{activeStage.inputs.map((item) => <li key={item}>{item}</li>)}</ul></div><div><h3>Outputs</h3><ul>{activeStage.outputs.map((item) => <li key={item}>{item}</li>)}</ul></div></div><h3>Exit criteria</h3><ul>{activeStage.exit.map((item) => <li key={item}>{item}</li>)}</ul><div className="rc-alert rc-alert-warning"><LockKeyhole size={15} /> Authority boundary: {activeStage.boundary}</div></div>}
                  </div>
                </Panel>
                <Panel title="Cross-stage orchestration and security controls" subtitle="Separate controls defined outside the eight primary lifecycle agents">
                  <div className="rc-comm-grid">
                    <div className="rc-comm-card"><strong>rusa_devops_orchestrator</strong><Badge tone="neutral">Cross-stage design</Badge><span>Workflow coordination</span><small>Splits work, manages dependencies, forwards stage outputs between agents and stops the workflow when a gate fails.</small></div>
                    <div className="rc-comm-card"><strong>rusa_security_guard_agent</strong><Badge tone="neutral">Cross-stage design</Badge><span>Cross-stage security guard</span><small>Checks secret scanning, dependency/container vulnerabilities, tenant isolation, authorization/audit, privacy, supply-chain provenance, model/tool permissions, prompt injection/untrusted input, and retention/deletion policy.</small></div>
                  </div>
                  <div className="rc-alert rc-alert-warning"><ShieldAlert size={16} /> Human approver is a decision-maker, not a ninth agent. The source document requires human approval for protected-branch merge, branch-protection/permission changes, production deployment where policy requires it, risky or irreversible migrations, data/resource deletion, secret/identity/security-policy changes, spend/capacity above budget, and publishing private assets to Social.</div>
                </Panel>
                <Panel title="Lifecycle gates and feedback loop" subtitle="Handoff rules from the proposed Agentic DevOps architecture">
                  <div className="rc-step-grid">{["PLAN", "CODE", "BUILD", "TEST", "RELEASE", "DEPLOY", "OPERATE", "MONITOR"].map((stage, index) => <div className="rc-step" key={stage}><span>{String(index + 1).padStart(2, "0")}</span><strong>{stage}</strong></div>)}</div>
                  <div className="rc-alert rc-alert-warning"><Workflow size={16} /> Code starts from the Plan Agent's defined scope, requirements, acceptance criteria, dependencies, and risk outputs; scope changes require product-owner approval. Required tests must pass before Release; the release approval required by policy gates Deploy; Monitor and Operate findings feed the next Plan. A failed gate stops the normal path and routes to repair or human review.</div>
                  <div className="rc-contract-grid"><div><strong>Evidence handoff</strong><span>Every handoff carries artifact and evidence references, not only a text summary.</span></div><div><strong>Re-test after repair</strong><span>Code repair creates a new commit/artifact; tests must rerun against that exact version. Old evidence cannot certify new changes.</span></div><div><strong>Auditability</strong><span>Decisions, retries, approvals, and cancellations are recorded.</span></div><div><strong>Feedback loop</strong><span>Monitor and Operate send recurring issues and improvements back to Plan.</span></div></div>
                </Panel>
                <div className="rc-alert rc-alert-warning"><AlertTriangle size={16} /> Delapan stage ini berstatus Proposed Architecture. Backend AICode saat ini menjalankan worker roles Analyst, Architect, Developer, Debugger, Tester, dan Security; belum ada lifecycle registry/API untuk mengklaim delapan stage tersebut aktif.</div>
              </div>
            )}

            {page === "chat" && (
              <Panel title="Inference workspace" subtitle="POST /v1/chat/completions · streaming requested when enabled">
                {!canUseChat && <div className="rc-alert rc-alert-warning">Role read_only tidak memiliki permission inference pada backend.</div>}
                <div className="rc-chat-toolbar"><label>Model<select value={chatModel} onChange={(event) => setChatModel(event.target.value)} disabled={!models.length}><option value="">{models.length ? "Select model" : "Model discovery unavailable"}</option>{models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label><label className="rc-toggle"><input type="checkbox" checked={chatStream} onChange={(event) => setChatStream(event.target.checked)} /> Request streaming</label><Badge tone={models.length ? "success" : "warning"}>{models.length ? models.length + " model(s)" : "Model unknown"}</Badge></div>
                {modelError && <div className="rc-note">Model discovery: {modelError}. Chat can use the backend-configured default model.</div>}
                <div className="rc-chat-history">{chatMessages.map((message, index) => <div className={"rc-chat-line " + message.role} key={index}><div className="rc-chat-avatar">{message.role === "assistant" ? <BrainCircuit size={16} /> : "U"}</div><div className="rc-chat-bubble"><small>{message.role === "assistant" ? "RUSA AI" : "YOU"}</small><p>{message.content || (chatBusy ? "RUSA AI sedang menulis…" : "")}</p></div></div>)}{chatBusy && <div className="rc-chat-working"><span className="rc-live-dot" /> Inference berjalan…</div>}</div>
                <form className="rc-chat-compose" onSubmit={sendChat}><textarea value={chatInput} maxLength={6000} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={canUseChat ? "Tulis pesan untuk RUSA AI…" : "Inference tidak tersedia untuk role ini"} disabled={!canUseChat || chatBusy} /><button className="rc-btn rc-btn-primary" type="submit" disabled={!canUseChat || chatBusy || !chatInput.trim()}><Send size={16} /> Send</button></form>
                <div className="rc-note">Model selector berasal dari GET /v1/models. Jika provider tidak mendukung stream (HTTP 501), UI otomatis mencoba non-streaming. UI mengirim maksimal 32 pesan terakhir dan 6.000 karakter per pesan dan batas serialisasi request 900 KiB agar tetap di bawah default body limit API 1 MiB. Chat tidak menjalankan AICode task.</div>
              </Panel>
            )}

            {page === "knowledge" && (
              <div className="rc-stack">
                <Panel title="Memory taxonomy" subtitle="Five categories defined in Memory & Knowledge Agent specification">
                  <div className="rc-memory-grid">{MEMORY_TYPES.map((item) => <article className="rc-memory-card" key={item.name}><div className="rc-agent-icon"><Database size={17} /></div><strong>{item.name}</strong><p>{item.detail}</p><small>{item.purpose}</small><Badge tone="neutral">Architecture contract</Badge></article>)}</div>
                </Panel>
                <Panel title="Implemented memory and knowledge components" subtitle="Separate the current runtime stores from the architecture taxonomy">
                  <div className="rc-contract-grid"><div><strong>AICode task memory</strong><span>SQLiteMemory for development/single-node and PostgresMemory for multi-worker production; tenant-scoped task history and prior worker results.</span></div><div><strong>Optional RAG knowledge store</strong><span>PostgreSQL + pgvector with vector/full-text hybrid retrieval; ingestion is available through the rusa-knowledge CLI.</span></div><div><strong>Read-only MCP tools</strong><span>search_knowledge · get_document · list_collections. Tenant identity comes from the authenticated bearer token, not a caller-supplied tenant ID.</span></div><div><strong>Worker integration</strong><span>Optional build/runtime capability; build Dockerfile.worker with RUSA_INSTALL_RAG=1 and set RUSA_RAG_ENABLED=true after the knowledge migration and restricted DB-role setup. It is not enabled by default or included in the default Compose services.</span></div></div>
                  <div className="rc-alert rc-alert-warning"><AlertTriangle size={16} /> The core web API has no generic memory CRUD or knowledge browser. RAG/MCP exists as an optional separate service and read-only worker tool surface; it does not implement all five memory categories as separate stores.</div>
                </Panel>
              </div>
            )}

            {page === "repository" && (
              <div className="rc-stack">
                <Panel title="Execution references" subtitle={"References extracted from " + loadedCount + " loaded task records"}>
                  {taskReferences.length ? <div className="rc-artifact-list">{taskReferences.map(({ task, reference }, index) => <div className="rc-artifact-row" key={task.task_id + reference.kind + index}><div className="rc-agent-icon"><Package size={17} /></div><div className="rc-grow"><strong>{reference.kind}</strong><small>{taskObjective(task)} · {task.task_id}</small>{reference.href ? <a className="rc-text-btn" href={reference.href} target="_blank" rel="noreferrer">{reference.value} <ArrowRight size={13} /></a> : <code className="rc-mono">{reference.value}</code>}</div><button className="rc-btn rc-btn-ghost" onClick={() => void openTask(task.task_id)}>Task details</button></div>)}</div> : <EmptyState title="No execution references in this page" detail="Successful task results expose references only after the runtime returns a pull request URL, commit SHA, or branch." />}
                </Panel>
                <Panel title="Evidence and artifact access boundary" subtitle="Current API visibility">
                  <div className="rc-contract-grid"><div><strong>Exposed in task result</strong><span>pull_request_url · commit_sha · branch</span></div><div><strong>Not exposed in task detail</strong><span>Per-worker evidence bundle, immutable artifact download, digest lookup, and provenance verification</span></div><div><strong>Runtime persistence</strong><span>Worker execution records are saved to tenant-scoped AICode memory; separate lifecycle audit events are recorded, but no browser-facing evidence API exists.</span></div><div><strong>Repository layer</strong><span>RUSA Repository remains the architecture evidence/artifact layer, not a file browser implemented by this UI.</span></div></div>
                </Panel>
              </div>
            )}

            {page === "evaluation" && (
              <div className="rc-stack">
                <div className="rc-kpi-grid">{KPI_GROUPS.map((group) => <Panel key={group.name} title={group.name} subtitle="Metric definitions from agent specification"><ul className="rc-kpi-list">{group.items.map((item) => <li key={item}><span>{item}</span><Badge tone="neutral">Defined</Badge></li>)}</ul></Panel>)}</div>
                <Panel title="Implemented evaluation and telemetry primitives" subtitle="These are narrower than the ten-agent Evaluation & Reporting role contract">
                  <div className="rc-contract-grid"><div><strong>Offline RAG retrieval evaluation</strong><span>rusa-knowledge evaluate computes Recall@K, MRR@K, and NDCG@K from a manually curated JSONL relevance dataset. This is an offline CLI, not a live dashboard.</span></div><div><strong>API / AICode Prometheus metrics</strong><span>API request count/duration and AICode task outcome/duration are instrumented in the API process.</span></div><div><strong>Worker metrics listener</strong><span>The worker process exposes its own Prometheus listener on port 9100; worker-step and tool-execution counters live in that separate process and are not aggregated into the API /metrics response.</span></div><div><strong>Not yet implemented as a reporting product</strong><span>Agent scorecard jobs, historical evaluation reports, model quality dashboards, cost attribution, and approved-improvement tracking are not exposed by the current web API.</span></div></div>
                </Panel>
                <div className="rc-alert rc-alert-warning"><Gauge size={16} /> The KPI groups above are definitions from the Evaluation & Reporting Agent specification. The page does not calculate or fabricate those scores.</div>
              </div>
            )}

            {page === "governance" && (
              <div className="rc-stack">
                <div className="rc-grid-two">
                  <Panel title="Authenticated principal" subtitle="Resolved from encrypted session via RUSA API">
                    <div className="rc-identity-list"><div><span>Subject</span><strong>{session.subject}</strong></div><div><span>Tenant ID</span><strong>{session.tenant_id}</strong></div><div><span>Roles</span><strong>{session.roles.join(", ")}</strong></div></div>
                  </Panel>
                  <Panel title="Task API permissions" subtitle="TASK_ROLE_PERMISSIONS from llm-project/serve/app.py">
                    <div className="rc-permission-list">{Object.entries(TASK_ROLE_PERMISSIONS).map(([role, permissions]) => <div key={role}><strong>{role}</strong><span>{permissions.join(" · ")}</span></div>)}</div>
                  </Panel>
                  <Panel title="Inference and operator access" subtitle="Separate backend checks; not part of task RBAC">
                    <div className="rc-permission-list"><div><strong>Inference</strong><span>{INFERENCE_ALLOWED_ROLES.join(" · ")}</span></div><div><strong>Operator metrics</strong><span>Role {METRICS_ALLOWED_ROLE} plus server-side RUSA_METRICS_API_KEY; the metrics API key is not a tenant API key.</span></div></div>
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
                  <Panel title="API process metrics" subtitle="GET /metrics · platform_admin plus server-side RUSA_METRICS_API_KEY">
                    {!isPlatformAdmin ? <EmptyState title="Operator permission required" detail="Metrics endpoint hanya tersedia untuk platform_admin." /> : metricsError ? <div className="rc-alert rc-alert-warning">{metricsError}</div> : <><div className="rc-metrics-meta"><Badge tone={metricsText ? "success" : "warning"}>{metricsText ? "Fetched" : "Not fetched"}</Badge><small>{metricsFetchedAt ? formatDate(metricsFetchedAt) : "No fetch timestamp"}</small></div><MetricPreview text={metricsText} /><button className="rc-btn rc-btn-ghost" onClick={() => void refreshMetrics()}><RefreshCw size={14} /> Refresh metrics</button></>}
                  </Panel>
                </div>
                <Panel title="Worker metrics listener" subtitle="Separate process and private scrape surface">
                  <div className="rc-contract-grid"><div><strong>Listener</strong><span>RUSA_METRICS_PORT defaults to 9100; Compose exposes port 9100 to attached container networks but does not publish it to the host.</span></div><div><strong>Scope</strong><span>Worker-step and tool-execution metrics are emitted by the worker process, not aggregated into the API process /metrics response.</span></div><div><strong>Web access</strong><span>No browser-facing worker-metrics proxy or aggregation endpoint is implemented in this Control Center.</span></div><div><strong>Security boundary</strong><span>Do not publish the worker metrics port publicly; scrape it only from an approved internal monitoring service.</span></div></div>
                </Panel>
                <Panel title="Telemetry delivery status" subtitle="Distinguish current behavior from ADR target">
                  <div className="rc-delivery-grid"><div><Badge tone="success">Current</Badge><strong>HTTP polling</strong><span>Task list: 10 seconds · Health: 30 seconds · Tenant total: 30 seconds</span></div><div><Badge tone="info">Current</Badge><strong>API metrics</strong><span>GET /metrics is the API process registry; worker metrics are a separate internal listener.</span></div><div><Badge tone="warning">Proposed</Badge><strong>WebSocket + cursor</strong><span>ADR-COMM-001 describes replay/reconnect; no external WebSocket endpoint is present in current app.</span></div><div><Badge tone="warning">Proposed</Badge><strong>Event log + outbox</strong><span>Kafka/outbox projection is an ADR target, not a verified deployment fact.</span></div></div>
                </Panel>
              </div>
            )}

            {page === "pillars" && (
              <div className="rc-stack">
                <div className="rc-pillar-detail-grid">{PILLARS.map((pillar, index) => <article className="rc-pillar-detail" key={pillar.name}><div className="rc-pillar-number">0{index + 1}</div><Badge tone={pillar.status === "Partial integration" ? "warning" : "neutral"}>{pillar.status}</Badge><h2>{pillar.name}</h2><p>{pillar.detail}</p><div className="rc-note">Evidence: {pillar.evidence}</div></article>)}</div>
                <div className="rc-alert rc-alert-warning"><Layers3 size={16} /> The four product pillars above follow RUSA_AI_Architecture.md. The DevOps lifecycle document separately names RUSA Repository and RUSA Social as delivery/evidence and optional publishing layers, while RUSA Platform Foundation is cross-cutting; those labels do not replace the four product pillars.</div>
              </div>
            )}

            {page === "developer" && (
              <div className="rc-stack">
                <Panel title="Model discovery" subtitle="GET /v1/models">
                  {models.length ? <div className="rc-model-list">{models.map((model) => <div key={model}><Sparkles size={16} /><strong>{model}</strong><Badge tone="success">API returned</Badge></div>)}</div> : <div className="rc-alert rc-alert-warning">{modelError || "No model IDs returned by RUSA API."}</div>}
                </Panel>
                <Panel title="A2A task envelope validator" subtitle="POST /v1/validate-task · JSON Schema Draft 2020-12">
                  <form onSubmit={validateEnvelope} className="rc-validator">
                    <label>Envelope JSON<textarea spellCheck={false} maxLength={262144} value={envelopeText} onChange={(event) => { setEnvelopeText(event.target.value); setValidation(null); }} /></label>
                    <div className="rc-toolbar"><button className="rc-btn rc-btn-primary" type="submit" disabled={validationBusy}>{validationBusy ? "Validating…" : "Validate envelope"} <ArrowRight size={14} /></button><span className="rc-note">This endpoint validates only schemas/a2a/task-envelope.schema.json (protocol_version=1.0). It does not validate the All Agents conceptual task contract, the ADR-COMM-001 proposed schema_version=1 envelope, or the AICode create-task request body.</span></div>
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
              <div className="rc-note rc-field-full">If the network response is uncertain, resubmitting the unchanged payload reuses its idempotency key. Editing the payload generates a new key.</div>
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
            <div className="rc-detail-section"><h3>Execution result payload</h3><pre className="rc-code">{jsonText(selectedTask.result || { status: "No result payload yet" })}</pre><div className="rc-alert rc-alert-warning"><FileCheck2 size={15} /> Successful AICode task results currently expose pull_request_url, commit_sha, and branch. Worker execution records are saved to tenant-scoped AICode memory; the current task-detail API does not expose those memory records or per-worker evidence.</div></div>
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
