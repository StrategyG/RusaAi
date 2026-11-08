import { createHash } from "node:crypto";
import { cookies } from "next/headers";
import { EncryptJWT, jwtDecrypt } from "jose";

export const SESSION_COOKIE = "rusa_session";
const SESSION_TTL_SECONDS = 60 * 60 * 8;

export type SessionIdentity = {
  subject: string;
  tenant_id: string;
  roles: string[];
};

export type ServerSession = {
  apiKey: string;
  subject: string;
  tenantId: string;
  roles: string[];
};

function encryptionKey(): Uint8Array {
  const secret = process.env.RUSA_WEB_SESSION_SECRET;
  if (!secret || secret.length < 32) {
    throw new Error("RUSA_WEB_SESSION_SECRET must contain at least 32 characters.");
  }
  return new Uint8Array(createHash("sha256").update(secret).digest());
}

export async function issueSessionToken(apiKey: string, identity: SessionIdentity): Promise<string> {
  return new EncryptJWT({
    apiKey,
    subject: identity.subject,
    tenantId: identity.tenant_id,
    roles: identity.roles
  })
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuedAt()
    .setExpirationTime("8h")
    .encrypt(encryptionKey());
}

export async function readServerSession(): Promise<ServerSession | null> {
  try {
    const cookieStore = await cookies();
    const token = cookieStore.get(SESSION_COOKIE)?.value;
    if (!token) return null;
    const { payload } = await jwtDecrypt(token, encryptionKey());
    if (
      typeof payload.apiKey !== "string" ||
      typeof payload.subject !== "string" ||
      typeof payload.tenantId !== "string" ||
      !Array.isArray(payload.roles) ||
      !payload.roles.every((role) => typeof role === "string")
    ) return null;
    return {
      apiKey: payload.apiKey,
      subject: payload.subject,
      tenantId: payload.tenantId,
      roles: payload.roles as string[]
    };
  } catch {
    return null;
  }
}

export function sessionCookieOptions() {
  return {
    httpOnly: true,
    secure:
      process.env.RUSA_WEB_COOKIE_SECURE === "true" ||
      (process.env.RUSA_WEB_COOKIE_SECURE !== "false" && process.env.NODE_ENV === "production"),
    sameSite: "lax" as const,
    path: "/",
    maxAge: SESSION_TTL_SECONDS
  };
}