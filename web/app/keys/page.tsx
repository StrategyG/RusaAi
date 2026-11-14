"use client";

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ArrowLeft, Check, Copy, KeyRound, Plus, ShieldCheck, Trash2 } from "lucide-react";

type ApiKey = {
  key_id: string;
  label: string;
  subject: string;
  roles: string[];
  created_at: string;
  revoked_at: string | null;
  is_bootstrap: boolean;
};

export default function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [label, setLabel] = useState("VS Code");
  const [role, setRole] = useState<"developer" | "read_only">("developer");
  const [issuedKey, setIssuedKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(async () => {
    const response = await fetch("/api/keys", { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      setAuthenticated(false);
      return;
    }
    if (!response.ok) throw new Error(data.detail || data.error || "API key inventory is unavailable.");
    setAuthenticated(true);
    setKeys(data.items || []);
  }, []);

  useEffect(() => {
    void refresh().catch((cause) => setError(cause instanceof Error ? cause.message : "Could not load API keys."));
  }, [refresh]);

  async function createKey(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    setIssuedKey("");
    try {
      const response = await fetch("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, roles: [role] })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || "Could not create editor key.");
      setIssuedKey(data.api_key);
      setNotice("Editor key created. Copy it now; it will not be shown again.");
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create editor key.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(keyId: string) {
    if (!window.confirm("Revoke this API key? Connected applications using it will stop working.")) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/keys/" + encodeURIComponent(keyId), { method: "DELETE" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || "Could not revoke key.");
      setNotice("API key revoked.");
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not revoke key.");
    } finally {
      setBusy(false);
    }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(issuedKey);
      setCopied(true);
    } catch {
      setError("Clipboard unavailable. Select and copy the key manually.");
    }
  }

  if (authenticated === false) {
    return <main className="min-h-screen bg-slate-950 p-8 text-slate-100"><div className="mx-auto max-w-xl rounded-xl border border-slate-700 bg-slate-900 p-6"><h1 className="text-xl font-semibold">Sign in required</h1><p className="mt-2 text-slate-300">Sign in to the RUSA AI dashboard with your local owner key before managing editor keys.</p><a className="mt-4 inline-block text-sky-300 underline" href="/">Open dashboard</a></div></main>;
  }

  return (
    <main className="min-h-screen bg-slate-950 px-5 py-8 text-slate-100">
      <div className="mx-auto max-w-4xl">
        <a href="/" className="inline-flex items-center gap-2 text-sm text-slate-400 hover:text-white"><ArrowLeft size={15} /> RUSA AI dashboard</a>
        <header className="mt-7 flex items-center gap-3"><div className="rounded-xl bg-sky-400/10 p-3 text-sky-300"><KeyRound size={24} /></div><div><h1 className="text-2xl font-semibold">API Keys</h1><p className="text-sm text-slate-400">Create a separate least-privilege key for each editor or client.</p></div></header>

        {error && <div role="alert" className="mt-5 rounded-lg border border-rose-400/30 bg-rose-400/5 p-3 text-sm text-rose-100">{error}</div>}
        {notice && <div role="status" aria-live="polite" className="mt-5 rounded-lg border border-emerald-400/30 bg-emerald-400/5 p-3 text-sm text-emerald-100">{notice}</div>}

        <section className="mt-6 rounded-xl border border-slate-700 bg-slate-900 p-5">
          <h2 className="font-semibold">Create editor API key</h2>
          <form onSubmit={createKey} className="mt-4 grid gap-3 sm:grid-cols-[1fr_180px_auto]">
            <input value={label} onChange={(event) => setLabel(event.target.value)} maxLength={80} placeholder="Editor name" className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm" required />
            <select value={role} onChange={(event) => setRole(event.target.value as "developer" | "read_only")} className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm">
              <option value="developer">Developer</option>
              <option value="read_only">Read only</option>
            </select>
            <button disabled={busy || !label.trim()} className="inline-flex items-center justify-center gap-2 rounded-lg bg-sky-500 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-50"><Plus size={15} /> Create key</button>
          </form>
          {issuedKey && <div className="mt-4 rounded-lg border border-emerald-400/30 bg-slate-950 p-4"><div className="flex items-center gap-2 text-sm font-semibold text-emerald-200"><ShieldCheck size={16} /> Key shown once</div><div className="mt-3 break-all font-mono text-sm">{issuedKey}</div><button onClick={() => void copyKey()} className="mt-3 inline-flex items-center gap-2 rounded-lg bg-emerald-300 px-3 py-2 text-xs font-semibold text-slate-950">{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "Copied" : "Copy key"}</button></div>}
        </section>

        <section className="mt-6 rounded-xl border border-slate-700 bg-slate-900 p-5">
          <h2 className="font-semibold">Existing keys</h2>
          <p className="mt-1 text-xs text-slate-400">Raw keys and SHA-256 digests are never returned by the inventory endpoint.</p>
          <div className="mt-4 divide-y divide-slate-800">
            {keys.map((item) => <div key={item.key_id} className="flex flex-wrap items-center justify-between gap-3 py-4">
              <div><div className="font-medium">{item.label} {item.is_bootstrap && <span className="ml-2 rounded bg-amber-400/10 px-2 py-1 text-[10px] text-amber-200">OWNER</span>}</div><div className="mt-1 text-xs text-slate-400">{item.roles.join(", ")} · {new Date(item.created_at).toLocaleString()}</div><div className="mt-1 font-mono text-[11px] text-slate-500">{item.key_id}</div>{item.revoked_at && <div className="mt-1 text-xs text-rose-300">Revoked</div>}</div>
              {!item.revoked_at && <button disabled={busy || item.is_bootstrap} onClick={() => void revoke(item.key_id)} className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40"><Trash2 size={14} /> Revoke</button>}
            </div>)}
            {keys.length === 0 && <div className="py-6 text-sm text-slate-400">No API keys found.</div>}
          </div>
        </section>
      </div>
    </main>
  );
}
