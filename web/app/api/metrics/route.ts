import { NextResponse } from "next/server";
import { backendBaseUrl, backendFetch } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const session = await readServerSession();
  if (!session) return NextResponse.json({ error: "Authentication required." }, { status: 401 });
  try {
    const identityResponse = await backendFetch("/v1/auth/session", session, {
      signal: AbortSignal.timeout(5000)
    });
    if (identityResponse.status === 401 || identityResponse.status === 403) {
      return NextResponse.json({ error: "Session is no longer authorized. Sign in again." }, { status: 401 });
    }
    if (!identityResponse.ok) {
      return NextResponse.json({ error: "RUSA API identity could not be revalidated." }, { status: 503 });
    }
    const identity = await identityResponse.json();
    if (
      identity.subject !== session.subject ||
      identity.tenant_id !== session.tenantId ||
      !Array.isArray(identity.roles)
    ) {
      return NextResponse.json({ error: "Session identity changed or is invalid. Sign in again." }, { status: 401 });
    }
    if (!identity.roles.includes("platform_admin")) {
      return NextResponse.json({ error: "Operator metrics require the current platform_admin role." }, { status: 403 });
    }
  } catch {
    return NextResponse.json({ error: "RUSA API identity service is unavailable." }, { status: 503 });
  }
  const metricsKey = process.env.RUSA_METRICS_API_KEY;
  if (!metricsKey) {
    return NextResponse.json({ error: "RUSA_METRICS_API_KEY is not configured for the web server." }, { status: 503 });
  }
  try {
    const response = await fetch(backendBaseUrl() + "/metrics", {
      headers: { "X-API-Key": metricsKey },
      cache: "no-store",
      signal: AbortSignal.timeout(10000)
    });
    const text = await response.text();
    if (!response.ok) {
      return NextResponse.json({ error: text || "Metrics endpoint rejected the request." }, { status: response.status });
    }
    return NextResponse.json({ text, fetched_at: new Date().toISOString() });
  } catch {
    return NextResponse.json({ error: "Metrics endpoint is unreachable." }, { status: 503 });
  }
}
