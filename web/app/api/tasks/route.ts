import { randomUUID } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  const params = new URLSearchParams();
  for (const key of ["status", "limit", "offset"]) {
    const value = request.nextUrl.searchParams.get(key);
    if (value) params.set(key, value);
  }
  const suffix = params.size ? "?" + params.toString() : "";
  const response = await backendFetch("/v1/aicode/tasks" + suffix, session);
  return forwardJson(response);
}

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
  const response = await backendFetch("/v1/aicode/tasks", session, {
    method: "POST",
    headers: { "Idempotency-Key": randomUUID() },
    body: JSON.stringify(body)
  });
  return forwardJson(response);
}