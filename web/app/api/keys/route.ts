import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

export async function GET() {
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  const response = await backendFetch("/v1/local/api-keys", session);
  return forwardJson(response);
}

export async function POST(request: NextRequest) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  let body: { label?: unknown; roles?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }
  if (
    typeof body.label !== "string" || !body.label.trim() || body.label.length > 80 ||
    !Array.isArray(body.roles) ||
    !body.roles.every((role) => role === "developer" || role === "read_only") ||
    body.roles.length < 1 || body.roles.length > 2
  ) {
    return NextResponse.json({ error: "Provide a label and least-privilege editor role." }, { status: 400 });
  }
  const response = await backendFetch("/v1/local/api-keys", session, {
    method: "POST",
    body: JSON.stringify({ label: body.label, roles: body.roles })
  });
  return forwardJson(response);
}
