import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

export async function POST(request: NextRequest) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }
  if (!body || typeof body !== "object" || Array.isArray(body) || !("envelope" in body)) {
    return NextResponse.json({ error: "An envelope object is required." }, { status: 400 });
  }
  const envelope = (body as { envelope?: unknown }).envelope;
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
    return NextResponse.json({ error: "Envelope must be a JSON object." }, { status: 400 });
  }
  if (new TextEncoder().encode(JSON.stringify(envelope)).byteLength > 262144) {
    return NextResponse.json({ error: "Envelope exceeds the 256 KiB web validator limit." }, { status: 413 });
  }
  const response = await backendFetch("/v1/validate-task", session, {
    method: "POST",
    body: JSON.stringify(body)
  });
  return forwardJson(response);
}
