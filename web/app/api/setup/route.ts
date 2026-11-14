import { NextRequest, NextResponse } from "next/server";
import { backendBaseUrl, forwardJson, sameOriginRequest } from "@/lib/backend";

export const runtime = "nodejs";

export async function GET() {
  try {
    const response = await fetch(backendBaseUrl() + "/v1/local/setup/status", {
      cache: "no-store",
      signal: AbortSignal.timeout(5000)
    });
    if (response.status === 404) {
      return NextResponse.json({ enabled: false, initialized: false });
    }
    if (!response.ok) {
      return NextResponse.json({ error: "Local setup status is unavailable." }, { status: 503 });
    }
    return forwardJson(response);
  } catch {
    return NextResponse.json({ error: "RUSA local API is not reachable." }, { status: 503 });
  }
}

export async function POST(request: NextRequest) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const setupToken = process.env.RUSA_LOCAL_SETUP_TOKEN || "";
  if (new TextEncoder().encode(setupToken).length < 32) {
    return NextResponse.json({ error: "Local first-run capability is not configured on the server." }, { status: 503 });
  }
  try {
    const response = await fetch(backendBaseUrl() + "/v1/local/setup/initialize", {
      method: "POST",
      headers: { "X-RUSA-Setup-Token": setupToken },
      cache: "no-store",
      signal: AbortSignal.timeout(10000)
    });
    return forwardJson(response);
  } catch {
    return NextResponse.json({ error: "Could not initialize the local RUSA API." }, { status: 503 });
  }
}
