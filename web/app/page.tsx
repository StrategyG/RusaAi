"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, AlertTriangle, Archive, ArrowRight, Bot, BrainCircuit, CheckCircle2,
  ChevronDown, Code2, Command, Database, FileText, Gauge, LayoutDashboard,
  LogOut, Menu, MessageSquare, Plus, RefreshCw, Send, ShieldAlert, ShieldCheck,
  Sparkles, Terminal, X
} from "lucide-react";

type Identity = { subject: string; tenant_id: string; roles: string[] };
type Task = {
  task_id: string; tenant_id: string; status: string; attempts: number; max_attempts: number;
  last_error?: string | null; created_at: string; updated_at: string;
  payload?: { objective?: string; context?: string; repository?: { full_name?: string; base_branch?: string }; acceptance_criteria?: string[] };
  result?: Record<string, unknown> | null;
};
type TaskPage = { items: Task[]; total: number; limit: number; offset: number };
type Health = { liveness: { ok: boolean; status: number; data?: Record<string, unknown> | null }; readiness: { ok: boolean; status: number; data?: Record<string, unknown> | null } };
type ChatLine = { role: "user" | "assistant"; content: string };

const navigation = [
  { label: "WORKSPACE", items: [{ id: "Overview", icon: LayoutDashboard }, { id: "Tasks", icon: Terminal }, { id: "Approvals", icon: ShieldCheck }] },
  { label: "INTELLIGENCE", items: [{ id: "Agents", icon: Bot }, { id: "Chat", icon: MessageSquare }, { id: "Artifacts", icon: Archive }] },
  { label: "PLATFORM", items: [{ id: "System", icon: Activity }] }
];

const runtimeAgents = [
  { name: "AICode Orchestrator", role: "Root supervisor", detail: "Mengatur urutan analyst, architect, engineering, testing, dan security; menyimpan hasil dan bukti." },
  { name: "Analyst", role: "Repository analysis", detail: "Menganalisis repository, struktur kode, konteks, dan kebutuhan task." },
  { name: "Architect", role: "Technical design", detail: "Menentukan pendekatan teknis dan dependency sebelum implementasi." },
  { name: "Developer", role: "Implementation", detail: "Mengubah source pada task worktree/feature branch melalui tool yang diizinkan." },
  { name: "Debugger", role: "Repair", detail: "Menganalisis kegagalan dan menyiapkan perbaikan terbatas." },
  { name: "Tester", role: "Verification", detail: "Menjalankan pengujian dan melaporkan evidence yang dapat diverifikasi." },
  { name: "Security", role: "Policy checks", detail: "Menjalankan pemeriksaan keamanan dan validasi bukti wajib." }
];

const lifecycleAgents = [
  ["Plan", "rusa_plan_agent"], ["Code", "rusa_code_agent"], ["Build", "rusa_build_agent"],
  ["Test", "rusa_test_agent"], ["Release", "rusa_release_agent"], ["Deploy", "rusa_deploy_agent"],
  ["Operate", "rusa_operate_agent"], ["Monitor", "rusa_monitor_agent"]
];

const productPillars = [
  { name: "AI Autonomous Software Engineering & DevOps", state: "AICode integration", detail: "Task intake, orchestrator, coding workers, task status, retry, and cancellation." },
  { name: "RUSA Intelligence", state: "Core API integration", detail: "Inference and AICode runtime are exposed here; memory and knowledge services remain governed by their own APIs." },
  { name: "RUSA Business", state: "Architecture pillar", detail: "Business operations automation, research, and company analysis." },
  { name: "RUSA Ecosystem", state: "Architecture pillar", detail: "Marketplace, developer API, external integrations, and agent ecosystem." }
];

const architectureRoles = [
  "Supreme Orchestrator", "System Architect", "Planner", "Task Decomposer",
  "Software Engineer", "DevOps", "QA & Testing", "Security & Policy",
  "Memory & Knowledge", "Evaluation & Reporting"
];

function statusClass(status: string) {
  const normalized = (status || "unknown").toLowerCase();
  const known = ["queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled", "blocked"];
  return "status status-" + (known.includes(normalized) ? normalized : "unknown");
}
function formatDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("id-ID", { dateStyle: "medium", timeStyle: "short" });
}
function errorText(data: any) {
  return data?.detail || data?.error || "Request could not be completed.";
}
function taskObjective(task: Task) {
  return task.payload?.objective || "Untitled task";
}

export default function Home() {
  const [session, setSession] = useState<Identity | null>(null);
  const [booting, setBooting] = useState(true);
  const [apiKey, setApiKey] = useState("");
  const [loginError, setLoginError] = useState("");
  const [page, setPage] = useState("Overview");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskTotal, setTaskTotal] = useState(0);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [taskFilter, setTaskFilter] = useState("all");
  const [objective, setObjective] = useState("");
  const [context, setContext] = useState("");
  const [repository, setRepository] = useState("StrategyG/RusaAi");
  const [baseBranch, setBaseBranch] = useState("main");
  const [criteria, setCriteria] = useState("Implement the requested change\nAdd or update relevant tests\nPreserve tenant isolation and policy controls");
  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatLine[]>([
    { role: "assistant", content: "RUSA AI siap menerima pertanyaan. Gunakan chat ini untuk inference; task coding dijalankan melalui AICode Tasks." }
  ]);

  const refreshTasks = useCallback(async () => {
    const response = await fetch("/api/tasks?limit=100", { cache: "no-store" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      setError(errorText(data));
      return;
    }
    const data: TaskPage = await response.json();
    setTasks(data.items || []);
    setTaskTotal(data.total || 0);
    setError("");
  }, []);

  const refreshHealth = useCallback(async () => {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (response.ok) setHealth(await response.json());
  }, []);

  const loadSession = useCallback(async () => {
    try {
      const response = await fetch("/api/session", { cache: "no-store" });
      if (response.ok) {
        setSession(await response.json());
        await Promise.all([refreshTasks(), refreshHealth()]);
      }
    } catch {
      setLoginError("RUSA API belum dapat dijangkau.");
    } finally {
      setBooting(false);
    }
  }, [refreshTasks, refreshHealth]);

  useEffect(() => { void loadSession(); }, [loadSession]);
  useEffect(() => {
    if (!session) return;
    const timer = window.setInterval(() => { void refreshTasks(); void refreshHealth(); }, 10000);
    return () => window.clearInterval(timer);
  }, [session, refreshTasks, refreshHealth]);

  const canCreate = session?.roles.some((role) => ["platform_admin", "tenant_admin", "developer"].includes(role)) ?? false;
  const canControl = session?.roles.some((role) => ["platform_admin", "tenant_admin"].includes(role)) ?? false;
  const canUseChat = Boolean(session);

  const counts = useMemo(() => ({
    active: tasks.filter((task) => ["queued", "running"].includes(task.status)).length,
    approvals: tasks.filter((task) => task.status === "awaiting_approval").length,
    succeeded: tasks.filter((task) => task.status === "succeeded").length,
    failed: tasks.filter((task) => ["failed", "blocked"].includes(task.status)).length
  }), [tasks]);

  const filteredTasks = useMemo(() => {
    if (taskFilter === "all") return tasks;
    if (taskFilter === "active") return tasks.filter((task) => ["queued", "running"].includes(task.status));
    return tasks.filter((task) => task.status === taskFilter);
  }, [tasks, taskFilter]);

  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setLoginError("");
    try {
      const response = await fetch("/api/session", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apiKey })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setSession(data);
      setApiKey("");
      await Promise.all([refreshTasks(), refreshHealth()]);
    } catch (cause) {
      setLoginError(cause instanceof Error ? cause.message : "Login failed.");
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    await fetch("/api/session", { method: "DELETE" });
    setSession(null);
    setTasks([]);
    setTaskTotal(0);
    setSelectedTask(null);
    setPage("Overview");
  }

  async function openTask(taskId: string) {
    setError("");
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId), { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setSelectedTask(data);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load task.");
    }
  }

  async function createTask(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    const acceptanceCriteria = criteria.split("\n").map((item) => item.trim()).filter(Boolean);
    try {
      const response = await fetch("/api/tasks", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          objective, context, repository: { full_name: repository, base_branch: baseBranch },
          acceptance_criteria: acceptanceCriteria
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNewTaskOpen(false);
      setObjective("");
      setContext("");
      setNotice("Task diterima dengan ID " + data.task_id + ".");
      setPage("Tasks");
      await refreshTasks();
      if (data.task_id) await openTask(data.task_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Task could not be created.");
    } finally {
      setBusy(false);
    }
  }

  async function cancelTask(taskId: string) {
    if (!window.confirm("Batalkan task ini? Tindakan yang sudah diteruskan ke sistem eksternal mungkin memerlukan rekonsiliasi.")) return;
    setBusy(true);
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId), { method: "DELETE" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNotice("Permintaan pembatalan diterima.");
      await refreshTasks();
      await openTask(taskId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Cancel failed.");
    } finally {
      setBusy(false);
    }
  }

  async function retryTask(taskId: string) {
    if (!window.confirm("Task hanya dapat di-retry setelah berstatus failed. Lanjutkan retry?")) return;
    setBusy(true);
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId) + "/retry", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNotice("Task dimasukkan kembali ke antrean.");
      await refreshTasks();
      await openTask(taskId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Retry failed.");
    } finally {
      setBusy(false);
    }
  }

  async function approveTask(taskId: string) {
    if (!window.confirm("Setujui hasil review manusia dan masukkan task kembali ke antrean?")) return;
    setBusy(true);
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId) + "/approve", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNotice("Persetujuan tercatat. Task kembali ke antrean.");
      await refreshTasks();
      await openTask(taskId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Approval failed.");
    } finally {
      setBusy(false);
    }
  }

  async function rejectTask(taskId: string) {
    if (!window.confirm("Tolak task ini? Task akan ditutup sebagai cancelled dan keputusan dicatat dalam audit trail.")) return;
    setBusy(true);
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(taskId) + "/reject", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      setNotice("Penolakan tercatat. Task ditutup.");
      await refreshTasks();
      await openTask(taskId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Rejection failed.");
    } finally {
      setBusy(false);
    }
  }

  async function sendChat(event: FormEvent) {
    event.preventDefault();
    const prompt = chatInput.trim();
    if (!prompt || chatBusy) return;
    const nextMessages = [...chatMessages, { role: "user" as const, content: prompt }];
    setChatMessages(nextMessages);
    setChatInput("");
    setChatBusy(true);
    try {
      const response = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: nextMessages.map((item) => ({ role: item.role, content: item.content })),
          temperature: 0.7, top_p: 0.9, max_tokens: 1024, stream: false
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(data));
      const answer = data.choices?.[0]?.message?.content || "Model returned an empty response.";
      setChatMessages((current) => [...current, { role: "assistant", content: answer }]);
    } catch (cause) {
      setChatMessages((current) => [...current, { role: "assistant", content: "Request gagal: " + (cause instanceof Error ? cause.message : "Unknown error") }]);
    } finally {
      setChatBusy(false);
    }
  }

  if (booting) {
    return <main className="login-wrap"><div className="muted text-sm">Memuat RUSA AI Control Center…</div></main>;
  }

  if (!session) {
    return (
      <main className="login-wrap">
        <section className="login-card">
          <div className="brand">
            <div className="brand-mark"><BrainCircuit size={23} /></div>
            <div><div className="brand-title">RUSA AI</div><div className="brand-subtitle">AGENTIC CONTROL CENTER</div></div>
          </div>
          <div className="eyebrow">SECURE WORKSPACE ACCESS</div>
          <h1>Welcome back.</h1>
          <p className="subtitle">Masuk menggunakan tenant-scoped RUSA API key. Kunci disimpan dalam sesi server terenkripsi dan tidak dikirim kembali ke browser.</p>
          <form onSubmit={login} className="mt-7">
            <div className="field">
              <label htmlFor="api-key">RUSA API Key</label>
              <input id="api-key" type="password" autoComplete="current-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="Masukkan API key" required />
            </div>
            {loginError && <div className="error-alert mb-4">{loginError}</div>}
            <button className="btn btn-primary btn-block" disabled={busy || !apiKey.trim()} type="submit">
              {busy ? "Connecting…" : "Connect to RUSA AI"} <ArrowRight size={15} />
            </button>
          </form>
          <div className="footer-note">Akses dan kemampuan ditentukan oleh tenant, roles, dan permission yang dikonfigurasi pada RUSA API. Jangan gunakan API key development pada production.</div>
        </section>
      </main>
    );
  }

  const pageSubtitle: Record<string, string> = {
    Overview: "Ringkasan task execution dan kesiapan layanan yang benar-benar dilaporkan oleh backend.",
    Tasks: "Buat dan pantau task coding; retry hanya tersedia untuk task yang gagal.",
    Approvals: "Task yang menunggu keputusan manusia; UI tidak melewati Policy Engine.",
    Agents: "Pisahkan worker yang ada di runtime saat ini dari target lifecycle arsitektur.",
    Chat: "Inference melalui OpenAI-compatible RUSA chat endpoint.",
    Artifacts: "Referensi output dan evidence yang dikembalikan bersama task.",
    System: "Liveness dan readiness dari RUSA API."
  };

  return (
    <main className="app-shell">
      <div className="shell">
        <aside className={"sidebar" + (mobileOpen ? " mobile-open" : "")}>
          <div className="brand">
            <div className="brand-mark"><BrainCircuit size={22} /></div>
            <div><div className="brand-title">RUSA AI</div><div className="brand-subtitle">AGENTIC CONTROL CENTER</div></div>
          </div>
          <div className="tenant-switch"><Command size={15} /><span className="truncate flex-1">{session.tenant_id}</span><ChevronDown size={13} /></div>
          {navigation.map((group) => (
            <div key={group.label}>
              <div className="nav-label">{group.label}</div>
              <div className="nav-list">
                {group.items.map((item) => {
                  const Icon = item.icon;
                  return (
                    <button key={item.id} className={"nav-item" + (page === item.id ? " active" : "")} onClick={() => { setPage(item.id); setMobileOpen(false); }}>
                      <Icon size={17} /><span>{item.id}</span>
                      {item.id === "Approvals" && counts.approvals > 0 && <span className="ml-auto rounded-full bg-amber-400/15 px-2 py-0.5 text-[10px] text-amber-300">{counts.approvals}</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
          <div className="sidebar-bottom">
            <div className="connection"><span className="dot" /> Session tenant-authenticated</div>
            <button className="nav-item" onClick={logout}><LogOut size={16} /><span>Sign out</span></button>
          </div>
        </aside>

        <section className="main-column">
          <header className="topbar">
            <div className="flex items-center gap-3">
              <button className="btn btn-quiet mobile-menu" onClick={() => setMobileOpen((value) => !value)} aria-label="Open navigation"><Menu size={18} /></button>
              <div className="breadcrumb">RUSA AI <span className="mx-2 text-slate-700">/</span> <strong>{page}</strong></div>
            </div>
            <div className="topbar-right">
              <span className="tenant-pill">{session.roles.join(" · ")}</span>
              <div className="avatar">{session.tenant_id.slice(0, 2).toUpperCase()}</div>
            </div>
          </header>

          <div className="content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">RUSA INTELLIGENCE / CONTROL PLANE</div>
                <h1>{page === "Overview" ? "Command Center" : page}</h1>
                <div className="subtitle">{pageSubtitle[page]}</div>
              </div>
              <div className="flex flex-wrap gap-2">
                <button className="btn" onClick={() => { void refreshTasks(); void refreshHealth(); }}><RefreshCw size={14} /> Refresh</button>
                {canCreate && <button className="btn btn-primary" onClick={() => setNewTaskOpen(true)}><Plus size={15} /> New AICode Task</button>}
              </div>
            </div>

            {error && <div className="error-alert mb-4">{error}</div>}
            {notice && <div className="success-alert mb-4">{notice}</div>}

            {page === "Overview" && (
              <>
                <div className="stat-grid">
                  <div className="stat-card"><div className="stat-top"><span>Total tasks</span><div className="stat-icon"><Terminal size={16} /></div></div><div className="stat-value">{taskTotal}</div><div className="stat-foot">Tenant-scoped records</div></div>
                  <div className="stat-card"><div className="stat-top"><span>Active</span><div className="stat-icon"><Activity size={16} /></div></div><div className="stat-value">{counts.active}</div><div className="stat-foot">Queued or running in loaded page</div></div>
                  <div className="stat-card"><div className="stat-top"><span>Awaiting approval</span><div className="stat-icon"><ShieldAlert size={16} /></div></div><div className="stat-value">{counts.approvals}</div><div className="stat-foot">Human review required</div></div>
                  <div className="stat-card"><div className="stat-top"><span>Failed / blocked</span><div className="stat-icon"><AlertTriangle size={16} /></div></div><div className="stat-value">{counts.failed}</div><div className="stat-foot">Requires investigation</div></div>
                </div>
                <section className="panel mb-4">
                  <div className="panel-header"><div><div className="panel-title">RUSA AI product pillars</div><div className="panel-subtitle">Four pillars from the RUSA AI architecture</div></div><BrainCircuit size={18} className="text-sky-300" /></div>
                  <div className="agent-grid">
                    {productPillars.map((pillar) => <article className="agent-card" key={pillar.name}><div className="flex items-center gap-2"><div className="list-icon"><BrainCircuit size={16} /></div><h3>{pillar.name}</h3></div><div className="agent-state">{pillar.state}</div><p>{pillar.detail}</p></article>)}
                  </div>
                </section>
                <div className="grid-two">
                  <section className="panel">
                    <div className="panel-header"><div><div className="panel-title">Recent task activity</div><div className="panel-subtitle">Latest tasks from the tenant-scoped queue</div></div><button className="btn btn-small" onClick={() => setPage("Tasks")}>View all <ArrowRight size={13} /></button></div>
                    {tasks.length === 0 ? <div className="empty"><Terminal size={25} /><div>No task records yet.</div><div className="mt-2">Create an AICode task to start the workflow.</div></div> : tasks.slice(0, 7).map((task) => (
                      <div className="list-row" key={task.task_id}>
                        <div className="list-icon"><Code2 size={16} /></div>
                        <div className="list-main"><button className="task-link" onClick={() => void openTask(task.task_id)}>{taskObjective(task)}</button><div className="list-desc"><span className="mono">{task.task_id.slice(0, 12)}</span> · {formatDate(task.updated_at)}</div></div>
                        <span className={statusClass(task.status)}>{task.status.replaceAll("_", " ")}</span>
                      </div>
                    ))}
                  </section>
                  <div className="stack">
                    <section className="panel">
                      <div className="panel-header"><div><div className="panel-title">API health</div><div className="panel-subtitle">Live checks, not simulated metrics</div></div><Gauge size={18} className="text-sky-300" /></div>
                      <div className="health-row"><span>API liveness</span><strong className={health?.liveness.ok ? "health-ok" : "health-bad"}>{health?.liveness.ok ? "Healthy" : "Unavailable"}</strong></div>
                      <div className="health-row"><span>Model / DB readiness</span><strong className={health?.readiness.ok ? "health-ok" : "health-bad"}>{health?.readiness.ok ? "Ready" : "Not ready"}</strong></div>
                      <div className="helper mt-3">Readiness depends on configured inference provider and database runtime.</div>
                    </section>
                    <section className="panel">
                      <div className="panel-header"><div><div className="panel-title">Runtime boundaries</div><div className="panel-subtitle">Current repository capabilities</div></div><ShieldCheck size={18} className="text-emerald-300" /></div>
                      <div className="list-row"><div className="list-icon"><Database size={15} /></div><div className="list-main"><div className="list-title">Tenant-scoped PostgreSQL queue</div><div className="list-desc">Durable task state and lease controls</div></div></div>
                      <div className="list-row"><div className="list-icon"><ShieldCheck size={15} /></div><div className="list-main"><div className="list-title">Policy-gated tools</div><div className="list-desc">Protected operations remain human-controlled</div></div></div>
                    </section>
                  </div>
                </div>
              </>
            )}

            {page === "Tasks" && (
              <section className="panel">
                <div className="panel-header"><div><div className="panel-title">AICode task queue</div><div className="panel-subtitle">{taskTotal} task records visible to this tenant</div></div>
                  <select className="search-input max-w-44" value={taskFilter} onChange={(event) => setTaskFilter(event.target.value)}>
                    <option value="all">All statuses</option><option value="active">Active</option><option value="queued">Queued</option><option value="running">Running</option><option value="awaiting_approval">Awaiting approval</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option>
                  </select>
                </div>
                <div className="table-wrap">
                  <table><thead><tr><th>Task</th><th>Repository</th><th>Status</th><th>Attempts</th><th>Updated</th></tr></thead>
                    <tbody>{filteredTasks.map((task) => <tr key={task.task_id}>
                      <td><button className="task-link" onClick={() => void openTask(task.task_id)}>{taskObjective(task)}</button><div className="mono mt-1">{task.task_id}</div></td>
                      <td>{task.payload?.repository?.full_name || "—"}</td><td><span className={statusClass(task.status)}>{task.status.replaceAll("_", " ")}</span></td>
                      <td>{task.attempts} / {task.max_attempts}</td><td>{formatDate(task.updated_at)}</td>
                    </tr>)}</tbody>
                  </table>
                  {filteredTasks.length === 0 && <div className="empty">No tasks match this filter.</div>}
                </div>
                <div className="helper mt-4">List API is tenant-filtered server-side. Refresh interval: 10 seconds. No broker or database is exposed to the browser.</div>
              </section>
            )}

            {page === "Approvals" && (
              <section className="panel">
                <div className="panel-header"><div><div className="panel-title">Human review queue</div><div className="panel-subtitle">Only tasks explicitly marked awaiting_approval appear here</div></div><ShieldAlert size={18} className="text-amber-300" /></div>
                <div className="inline-alert mb-4">Review hasil dan evidence sebelum mengambil keputusan. Approve akan mencatat keputusan manusia lalu melanjutkan task; Reject menutup task sebagai cancelled. Retry terpisah dan hanya berlaku untuk task berstatus failed.</div>
                {tasks.filter((task) => task.status === "awaiting_approval").length === 0
                  ? <div className="empty"><CheckCircle2 size={25} /><div>No pending approvals.</div></div>
                  : tasks.filter((task) => task.status === "awaiting_approval").map((task) => (
                    <div className="list-row" key={task.task_id}>
                      <div className="list-icon"><ShieldAlert size={16} /></div>
                      <div className="list-main"><button className="task-link" onClick={() => void openTask(task.task_id)}>{taskObjective(task)}</button><div className="list-desc">{task.task_id} · {task.payload?.repository?.full_name || "Repository not provided"}</div></div>
                      <span className={statusClass(task.status)}>Awaiting approval</span>
                      {canControl && <button className="btn btn-small btn-primary" disabled={busy} onClick={() => void approveTask(task.task_id)}><CheckCircle2 size={13} /> Approve</button>}
                      {canControl && <button className="btn btn-small btn-danger" disabled={busy} onClick={() => void rejectTask(task.task_id)}><X size={13} /> Reject</button>}
                    </div>
                  ))}
              </section>
            )}

            {page === "Agents" && (
              <div className="stack">
                <section className="panel">
                  <div className="panel-header"><div><div className="panel-title">Current AICode runtime</div><div className="panel-subtitle">Roles and orchestrator present in the repository implementation</div></div><span className="status status-succeeded">Code-defined</span></div>
                  <div className="agent-grid">
                    {runtimeAgents.map((agent) => <article className="agent-card" key={agent.name}><div className="flex items-center gap-2"><div className="list-icon"><Bot size={16} /></div><h3>{agent.name}</h3></div><div className="agent-state">{agent.role}</div><p>{agent.detail}</p></article>)}
                  </div>
                </section>
                <section className="panel">
                  <div className="panel-header"><div><div className="panel-title">RUSA AI architecture catalogue</div><div className="panel-subtitle">Roles named in the All Agents architecture document; shown separately from runtime registrations</div></div><span className="status status-queued">Design catalogue</span></div>
                  <div className="agent-grid">
                    {architectureRoles.map((name) => <article className="agent-card" key={name}><div className="flex items-center gap-2"><div className="list-icon"><BrainCircuit size={16} /></div><h3>{name}</h3></div><div className="agent-state">Architecture role</div><p>Defined in the RUSA AI agent catalogue. Runtime binding and canonical agent identity are not inferred by the UI.</p></article>)}
                  </div>
                  <div className="inline-alert mt-4">The source documents use Supreme Orchestrator, Executive Orchestrator, and DevOps Orchestrator terminology. Their canonical identity/ownership mapping remains an architecture decision; this interface keeps the labels distinct.</div>
                </section>
                <section className="panel">
                  <div className="panel-header"><div><div className="panel-title">Agentic DevOps lifecycle target</div><div className="panel-subtitle">Eight specialized stages from the architecture document; not all are exposed as runtime agents in this repository yet</div></div><span className="status status-queued">Architecture target</span></div>
                  <div className="agent-grid">
                    {lifecycleAgents.map(([name, id], index) => <article className="agent-card" key={id}><div className="flex items-center gap-2"><div className="list-icon"><span className="mono">{String(index + 1).padStart(2, "0")}</span></div><h3>{name}</h3></div><div className="agent-state">{id}</div><p>Lifecycle capability defined in the RUSA Agentic DevOps design. Runtime/API integration is a separate implementation milestone.</p></article>)}
                  </div>
                  <div className="inline-alert mt-4">Security Guard is a cross-cutting control across lifecycle stages. The current API exposes security worker capability, but a distinct lifecycle Security Guard runtime identity is not yet registered here.</div>
                </section>
              </div>
            )}

            {page === "Chat" && (
              <section className="panel chat-panel">
                <div className="panel-header"><div><div className="panel-title">RUSA AI Chat</div><div className="panel-subtitle">POST /v1/chat/completions · non-streaming mode</div></div><Sparkles size={18} className="text-sky-300" /></div>
                {!canUseChat && <div className="inline-alert mb-3">Permintaan inference tetap divalidasi oleh autentikasi dan rate limit RUSA API.</div>}
                <div className="chat-history">
                  {chatMessages.map((message, index) => <div className={"chat-bubble " + message.role} key={index}>{message.content}</div>)}
                  {chatBusy && <div className="chat-bubble assistant">RUSA AI sedang memproses…</div>}
                </div>
                <form className="chat-compose" onSubmit={sendChat}>
                  <textarea value={chatInput} onChange={(event) => setChatInput(event.target.value)} placeholder="Tanyakan sesuatu kepada RUSA AI…" disabled={!canUseChat || chatBusy} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendChat(event as unknown as FormEvent); } }} />
                  <button className="btn btn-primary" type="submit" disabled={!canUseChat || chatBusy || !chatInput.trim()} aria-label="Send message"><Send size={16} /></button>
                </form>
              </section>
            )}

            {page === "Artifacts" && (
              <section className="panel">
                <div className="panel-header"><div><div className="panel-title">Task artifacts & evidence</div><div className="panel-subtitle">Artifacts are surfaced from task result payloads</div></div><Archive size={18} className="text-sky-300" /></div>
                <div className="inline-alert mb-4">Repository architecture defines immutable artifact references, evidence, and provenance. The current public API exposes task result payloads; a dedicated repository browsing API is not present yet.</div>
                {tasks.map((task) => {
                  const result = task.result || {};
                  const artifacts = Array.isArray(result.artifacts) ? result.artifacts : [];
                  const evidence = result.evidence && typeof result.evidence === "object" ? Object.keys(result.evidence as object) : [];
                  if (artifacts.length === 0 && evidence.length === 0) return null;
                  return <div className="list-row" key={task.task_id}><div className="list-icon"><FileText size={16} /></div><div className="list-main"><button className="task-link" onClick={() => void openTask(task.task_id)}>{taskObjective(task)}</button><div className="list-desc">{artifacts.length} artifact references · {evidence.length} evidence keys</div></div><span className="mono">{task.task_id.slice(0, 12)}</span></div>;
                })}
                {!tasks.some((task) => {
                  const result = task.result || {};
                  return (Array.isArray(result.artifacts) && result.artifacts.length > 0) || (result.evidence && typeof result.evidence === "object" && Object.keys(result.evidence as object).length > 0);
                }) && <div className="empty"><Archive size={25} /><div>No artifact references in the loaded task page.</div></div>}
              </section>
            )}

            {page === "System" && (
              <div className="grid-two">
                <section className="panel">
                  <div className="panel-header"><div><div className="panel-title">Service health</div><div className="panel-subtitle">Read-only health endpoints</div></div><Activity size={18} className="text-sky-300" /></div>
                  <div className="health-row"><span>GET /health</span><strong className={health?.liveness.ok ? "health-ok" : "health-bad"}>{health?.liveness.ok ? "Healthy" : "Unavailable"}</strong></div>
                  <div className="health-row"><span>GET /health/ready</span><strong className={health?.readiness.ok ? "health-ok" : "health-bad"}>{health?.readiness.ok ? "Ready" : "Not ready"}</strong></div>
                  <pre className="json mt-4">{JSON.stringify(health, null, 2)}</pre>
                </section>
                <section className="panel">
                  <div className="panel-header"><div><div className="panel-title">Authenticated identity</div><div className="panel-subtitle">Resolved by RUSA API key principal</div></div><ShieldCheck size={18} className="text-emerald-300" /></div>
                  <div className="detail-cell mb-2"><div className="detail-label">Tenant</div><div className="detail-value">{session.tenant_id}</div></div>
                  <div className="detail-cell mb-2"><div className="detail-label">Subject</div><div className="detail-value">{session.subject}</div></div>
                  <div className="detail-cell"><div className="detail-label">Roles</div><div className="detail-value">{session.roles.join(", ")}</div></div>
                  <div className="footer-note">The UI never receives database credentials, worker leases, model provider secrets, or broker access.</div>
                </section>
              </div>
            )}
            <div className="footer-note">RUSA AI Control Center · API-backed values only · Task list refreshes every 10 seconds · No production deployment is triggered by this interface.</div>
          </div>
        </section>
      </div>

      {newTaskOpen && (
        <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setNewTaskOpen(false); }}>
          <form className="modal" onSubmit={createTask}>
            <div className="modal-header"><div><div className="eyebrow">AICODE / TASK INTAKE</div><div className="modal-title mt-1">Create engineering task</div><div className="panel-subtitle">Task akan masuk ke durable queue RUSA AI.</div></div><button className="btn btn-quiet" type="button" onClick={() => setNewTaskOpen(false)} aria-label="Close"><X size={17} /></button></div>
            <div className="field"><label>Objective</label><textarea value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="Jelaskan hasil yang ingin dicapai…" required maxLength={8000} /></div>
            <div className="grid grid-cols-2 gap-3">
              <div className="field"><label>Repository (owner/name)</label><input value={repository} onChange={(event) => setRepository(event.target.value)} required /></div>
              <div className="field"><label>Base branch</label><input value={baseBranch} onChange={(event) => setBaseBranch(event.target.value)} required /></div>
            </div>
            <div className="field"><label>Context (optional)</label><textarea value={context} onChange={(event) => setContext(event.target.value)} placeholder="Relevant architecture, constraints, or references…" maxLength={20000} /></div>
            <div className="field"><label>Acceptance criteria (one per line)</label><textarea value={criteria} onChange={(event) => setCriteria(event.target.value)} required /></div>
            {error && <div className="error-alert mb-3">{error}</div>}
            <div className="flex justify-end gap-2"><button className="btn" type="button" onClick={() => setNewTaskOpen(false)}>Cancel</button><button className="btn btn-primary" type="submit" disabled={busy || !objective.trim() || !repository.trim() || !baseBranch.trim() || !criteria.trim()}>{busy ? "Submitting…" : "Submit task"} <ArrowRight size={14} /></button></div>
          </form>
        </div>
      )}

      {selectedTask && (
        <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelectedTask(null); }}>
          <section className="modal modal-wide">
            <div className="modal-header"><div><div className="eyebrow">TASK DETAIL</div><div className="modal-title mt-1">{taskObjective(selectedTask)}</div><div className="mono mt-2">{selectedTask.task_id}</div></div><button className="btn btn-quiet" onClick={() => setSelectedTask(null)} aria-label="Close"><X size={17} /></button></div>
            <div className="flex items-center justify-between gap-3"><span className={statusClass(selectedTask.status)}>{selectedTask.status.replaceAll("_", " ")}</span><span className="helper">Updated {formatDate(selectedTask.updated_at)}</span></div>
            <div className="detail-grid">
              <div className="detail-cell"><div className="detail-label">Repository</div><div className="detail-value">{selectedTask.payload?.repository?.full_name || "—"}</div></div>
              <div className="detail-cell"><div className="detail-label">Attempts</div><div className="detail-value">{selectedTask.attempts} / {selectedTask.max_attempts}</div></div>
              <div className="detail-cell"><div className="detail-label">Tenant</div><div className="detail-value">{selectedTask.tenant_id}</div></div>
              <div className="detail-cell"><div className="detail-label">Created</div><div className="detail-value">{formatDate(selectedTask.created_at)}</div></div>
            </div>
            {selectedTask.last_error && <div className="error-alert mb-3">{selectedTask.last_error}</div>}
            <div className="panel-subtitle mb-2">Task result / execution evidence</div>
            <pre className="json">{JSON.stringify(selectedTask.result || { message: "No result has been recorded yet." }, null, 2)}</pre>
            <div className="flex flex-wrap justify-end gap-2 mt-4">
              {canControl && selectedTask.status === "failed" && <button className="btn" disabled={busy} onClick={() => void retryTask(selectedTask.task_id)}><RefreshCw size={14} /> Retry</button>}
              {canControl && selectedTask.status === "awaiting_approval" && <button className="btn btn-primary" disabled={busy} onClick={() => void approveTask(selectedTask.task_id)}><CheckCircle2 size={14} /> Approve</button>}
              {canControl && selectedTask.status === "awaiting_approval" && <button className="btn btn-danger" disabled={busy} onClick={() => void rejectTask(selectedTask.task_id)}><X size={14} /> Reject</button>}
              {canControl && ["queued", "running"].includes(selectedTask.status) && <button className="btn btn-danger" disabled={busy} onClick={() => void cancelTask(selectedTask.task_id)}>Cancel task</button>}
              <button className="btn" onClick={() => setSelectedTask(null)}>Close</button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}