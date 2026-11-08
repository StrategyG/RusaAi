import { NextResponse } from "next/server";
import { backendBaseUrl } from "@/lib/backend";

export const runtime = "nodejs";

export async function GET() {
  const base = backendBaseUrl();
  const [live, ready] = await Promise.allSettled([
    fetch(base + "/health", { cache: "no-store", signal: AbortSignal.timeout(5000) }),
    fetch(base + "/health/ready", { cache: "no-store", signal: AbortSignal.timeout(5000) })
  ]);

  const liveness = live.status === "fulfilled"
    ? { ok: live.value.ok, status: live.value.status, data: await live.value.json().catch(() => null) }
    : { ok: false, status: 0, data: null };
  const readiness = ready.status === "fulfilled"
    ? { ok: ready.value.ok, status: ready.value.status, data: await ready.value.json().catch(() => null) }
    : { ok: false, status: 0, data: null };

  return NextResponse.json({ liveness, readiness });
}