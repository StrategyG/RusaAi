import { NextResponse } from "next/server";
import { backendBaseUrl } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const session = await readServerSession();
  if (!session) return NextResponse.json({ error: "Authentication required." }, { status: 401 });
  if (!session.roles.includes("platform_admin")) {
    return NextResponse.json({ error: "Operator metrics require platform_admin." }, { status: 403 });
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
