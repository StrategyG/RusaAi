"use client";

import { useEffect, useMemo, useState } from "react";
import { ArrowRight, BrainCircuit, Check, Copy, KeyRound, ShieldCheck, Sparkles } from "lucide-react";

type SetupStatus = { enabled: boolean; initialized: boolean };
type IssuedKey = {
  api_key: string;
  api_base_url: string;
  model_name: string;
  key_display_policy: string;
};

const LOCAL_API_BASE = process.env.NEXT_PUBLIC_RUSA_LOCAL_API_BASE_URL || "http://127.0.0.1:8000/v1";

export default function LocalSetupPage() {
  const [status, setStatus] = useState<"loading" | "ready" | "complete" | "disabled" | "error">("loading");
  const [issued, setIssued] = useState<IssuedKey | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");

  useEffect(() => {
    let active = true;
    fetch("/api/setup", { cache: "no-store" })
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "Local setup is unavailable.");
        if (!active) return;
        if (!data.enabled) setStatus("disabled");
        else setStatus(data.initialized ? "complete" : "ready");
      })
      .catch((cause) => {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "Could not reach local setup.");
        setStatus("error");
      });
    return () => { active = false; };
  }, []);

  const editorConfig = useMemo(() => {
    if (!issued) return "";
    return JSON.stringify({
      provider: "openai-compatible",
      base_url: LOCAL_API_BASE,
      api_key: issued.api_key,
      model: issued.model_name || "rusaai-cpu-smoke-test"
    }, null, 2);
  }, [issued]);

  async function initialize() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/setup", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || "Local setup failed.");
      setIssued(data as IssuedKey);
      setStatus("complete");
      // Establish an encrypted HttpOnly dashboard session without persisting
      // the raw key in browser storage.
      await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apiKey: data.api_key })
      }).catch(() => undefined);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Local setup failed.");
    } finally {
      setBusy(false);
    }
  }

  async function copyValue(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
      window.setTimeout(() => setCopied(""), 1800);
    } catch {
      setError("Clipboard access is unavailable. Select and copy the value manually.");
    }
  }

  return (
    <main className="min-h-screen bg-slate-950 px-5 py-10 text-slate-100">
      <div className="mx-auto max-w-3xl">
        <header className="mb-10 flex items-center gap-3">
          <div className="rounded-xl border border-sky-400/20 bg-sky-400/10 p-3 text-sky-300"><BrainCircuit size={26} /></div>
          <div><div className="text-xl font-semibold tracking-wide">RUSA AI</div><div className="text-xs tracking-[.22em] text-slate-400">LOCAL FIRST-RUN SETUP</div></div>
        </header>

        <section className="rounded-2xl border border-slate-700 bg-slate-900/80 p-6 shadow-2xl sm:p-9">
          <div className="mb-4 flex items-center gap-2 text-xs font-semibold tracking-[.18em] text-emerald-300"><Sparkles size={15} /> LOCAL INSTALLATION</div>
          <h1 className="text-3xl font-semibold">RUSA AI is almost ready.</h1>
          <p className="mt-3 max-w-2xl leading-7 text-slate-300">
            Create the local owner credential once. The raw API key is shown only at creation time.
            Keep it private and use separate least-privilege keys for individual editors. After setup, sign in to manage those keys at /keys.
          </p>

          {status === "loading" && <div className="mt-8 text-sm text-slate-400">Checking local services…</div>}

          {status === "ready" && (
            <div className="mt-8 rounded-xl border border-slate-700 bg-slate-950/70 p-5">
              <div className="flex items-center gap-3"><ShieldCheck className="text-emerald-300" /><div className="font-medium">Local API is ready for first-run setup</div></div>
              <p className="mt-2 text-sm text-slate-400">RUSA AI will create one owner key and store only its SHA-256 digest.</p>
              <button onClick={initialize} disabled={busy} className="mt-5 inline-flex items-center gap-2 rounded-lg bg-sky-500 px-5 py-3 font-semibold text-slate-950 transition hover:bg-sky-400 disabled:opacity-50">
                {busy ? "Creating secure API key…" : "Create my RUSA API key"} <ArrowRight size={16} />
              </button>
            </div>
          )}

          {status === "complete" && !issued && (
            <div className="mt-8 rounded-xl border border-amber-400/30 bg-amber-400/5 p-5">
              <div className="font-medium text-amber-200">This local installation has already been initialized.</div>
              <p className="mt-2 text-sm text-slate-300">For security, RUSA AI cannot reveal the original API key again. Sign in with the key you saved, or use the local key-management process to issue a replacement.</p>
              <a href="/" className="mt-4 inline-flex items-center gap-2 rounded-lg border border-slate-600 px-4 py-2 text-sm hover:bg-slate-800">Open RUSA AI dashboard <ArrowRight size={15} /></a>
            </div>
          )}

          {issued && (
            <div className="mt-8 space-y-5">
              <div className="rounded-xl border border-emerald-400/30 bg-emerald-400/5 p-5">
                <div className="flex items-center gap-2 font-semibold text-emerald-200"><Check size={18} /> Local API key created</div>
                <p className="mt-2 text-sm text-slate-300">Copy and store this key now. It will not be displayed again.</p>
                <div className="mt-4 break-all rounded-lg border border-slate-700 bg-slate-950 p-4 font-mono text-sm">{issued.api_key}</div>
                <button onClick={() => void copyValue("key", issued.api_key)} className="mt-3 inline-flex items-center gap-2 rounded-lg bg-emerald-300 px-4 py-2 text-sm font-semibold text-slate-950">
                  {copied === "key" ? <Check size={15} /> : <Copy size={15} />} {copied === "key" ? "Copied" : "Copy API key"}
                </button>
              </div>

              <div className="rounded-xl border border-slate-700 bg-slate-950/70 p-5">
                <div className="flex items-center justify-between gap-3"><div className="font-semibold">Editor connection</div><KeyRound size={18} className="text-sky-300" /></div>
                <div className="mt-3 text-xs uppercase tracking-wider text-slate-400">API Base URL</div>
                <div className="mt-1 break-all font-mono text-sm">{LOCAL_API_BASE}</div>
                <button onClick={() => void copyValue("url", LOCAL_API_BASE)} className="mt-2 inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-xs hover:bg-slate-800">
                  {copied === "url" ? <Check size={14} /> : <Copy size={14} />} Copy endpoint
                </button>
                <div className="mt-5 text-xs uppercase tracking-wider text-slate-400">Generic OpenAI-compatible client configuration</div>
                <pre className="mt-2 overflow-x-auto rounded-lg border border-slate-700 bg-slate-950 p-4 text-xs leading-6 text-slate-200">{editorConfig}</pre>
                <button onClick={() => void copyValue("config", editorConfig)} className="mt-2 inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-xs hover:bg-slate-800">
                  {copied === "config" ? <Check size={14} /> : <Copy size={14} />} Copy editor configuration
                </button>
              </div>

              <div className="rounded-xl border border-slate-700 bg-slate-950/70 p-5 text-sm text-slate-300">
                <strong className="text-white">Important:</strong> API authentication is ready, but chat inference becomes ready only after a verified RusaModels model runtime and approved model artifact are provisioned. Editor-specific integrations may require an extension or adapter.
              </div>
              <div className="flex flex-wrap gap-3">
                <a href="/" className="inline-flex items-center gap-2 rounded-lg bg-sky-500 px-5 py-3 font-semibold text-slate-950 hover:bg-sky-400">Continue to RUSA AI <ArrowRight size={16} /></a>
                <a href="/keys" className="inline-flex items-center gap-2 rounded-lg border border-slate-600 px-5 py-3 text-sm font-semibold text-slate-200 hover:bg-slate-800">Manage editor keys</a>
              </div>
            </div>
          )}

          {status === "disabled" && <div className="mt-8 rounded-lg border border-amber-400/30 bg-amber-400/5 p-4 text-sm text-amber-100">Local first-run mode is disabled. Enable it only in the local desktop deployment profile.</div>}
          {status === "error" && <div className="mt-8 rounded-lg border border-rose-400/30 bg-rose-400/5 p-4 text-sm text-rose-100">{error}</div>}
          {error && status !== "error" && <div className="mt-4 rounded-lg border border-rose-400/30 bg-rose-400/5 p-3 text-sm text-rose-100">{error}</div>}
        </section>
        <p className="mt-5 text-center text-xs text-slate-500">Local API keys stay on this device. Never share them in source code, logs, screenshots or public repositories.</p>
      </div>
    </main>
  );
}
