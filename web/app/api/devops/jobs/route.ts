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
  return forwardJson(await backendFetch("/v1/devops/jobs" + suffix, session));
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
  return forwardJson(await backendFetch("/v1/devops/jobs", session, {
    method: "POST", body: JSON.stringify(body)
  }));
}
