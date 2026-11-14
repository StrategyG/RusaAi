import { NextRequest, NextResponse } from "next/server";
import { backendFetch, forwardJson, sameOriginRequest, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";

type RouteContext = { params: Promise<{ keyId: string }> };

export async function DELETE(request: NextRequest, context: RouteContext) {
  if (!sameOriginRequest(request)) {
    return NextResponse.json({ error: "Cross-origin request rejected." }, { status: 403 });
  }
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  const { keyId } = await context.params;
  const response = await backendFetch("/v1/local/api-keys/" + encodeURIComponent(keyId), session, {
    method: "DELETE"
  });
  return forwardJson(response);
}
