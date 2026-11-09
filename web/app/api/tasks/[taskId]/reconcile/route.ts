import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

type RouteContext = { params: Promise<{ taskId: string }> };

export async function POST(request: NextRequest, context: RouteContext) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();

  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > 4096) {
    return NextResponse.json({ error: "Reconciliation request is too large." }, { status: 413 });
  }

  const { taskId } = await context.params;
  const response = await backendFetch(
    "/v1/aicode/tasks/" + encodeURIComponent(taskId) + "/reconcile",
    session,
    { method: "POST", body: raw }
  );
  return forwardJson(response);
}
