import { NextResponse } from "next/server";
import { backendFetch, forwardJson, unauthorizedResponse } from "@/lib/backend";
import { readServerSession } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const session = await readServerSession();
  if (!session) return unauthorizedResponse();
  const response = await backendFetch("/v1/models", session);
  return forwardJson(response);
}
