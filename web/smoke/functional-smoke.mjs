import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { cpSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "@playwright/test";

const webDir = resolve(import.meta.dirname, "..");
const standaloneDir = resolve(webDir, ".next", "standalone");
const standaloneServer = resolve(standaloneDir, "server.js");

if (!existsSync(standaloneServer)) {
  throw new Error("Next.js standalone server is missing. Run npm run build before this smoke test.");
}
if (existsSync(resolve(webDir, ".next", "static"))) {
  cpSync(resolve(webDir, ".next", "static"), resolve(standaloneDir, ".next", "static"), { recursive: true, force: true });
}
if (existsSync(resolve(webDir, "public"))) {
  cpSync(resolve(webDir, "public"), resolve(standaloneDir, "public"), { recursive: true, force: true });
}

const now = "2026-11-09T00:00:00.000Z";
const seededTask = {
  task_id: "00000000-0000-4000-8000-000000000001",
  tenant_id: "tenant-smoke",
  status: "failed",
  attempts: 1,
  max_attempts: 3,
  created_at: now,
  updated_at: now,
  payload: {
    objective: "Smoke-test task detail",
    repository: { full_name: "StrategyG/RusaAi", base_branch: "main" },
    acceptance_criteria: ["The BFF returns the task"]
  },
  result: { branch: "feature/smoke", commit_sha: "0123456789abcdef0123456789abcdef01234567" }
};
const approvalTask = { ...seededTask, task_id: "00000000-0000-4000-8000-000000000002", status: "awaiting_approval" };
const seen = [];

function json(response, status, body) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify(body));
}
async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}
function identityFor(key) {
  if (key === "platform-key") return { subject: "smoke-admin", tenant_id: "tenant-smoke", roles: ["platform_admin"] };
  if (key === "developer-key") return { subject: "smoke-developer", tenant_id: "tenant-smoke", roles: ["developer"] };
  if (key === "readonly-key") return { subject: "smoke-reader", tenant_id: "tenant-smoke", roles: ["read_only"] };
  return null;
}

const backend = createServer(async (request, response) => {
  try {
    const url = new URL(request.url || "/", "http://127.0.0.1");
    const apiKey = String(request.headers["x-api-key"] || "");
    seen.push({ method: request.method, path: url.pathname + url.search, apiKey });

    if (url.pathname === "/v1/auth/session" && request.method === "GET") {
      const identity = identityFor(apiKey);
      return identity ? json(response, 200, identity) : json(response, 401, { detail: "Invalid API key" });
    }
    if (url.pathname === "/health" && request.method === "GET") {
      return json(response, 200, { status: "healthy", service: "rusa-ai-smoke" });
    }
    if (url.pathname === "/health/ready" && request.method === "GET") {
      return json(response, 200, { status: "ready", inference: "reachable", database: "reachable" });
    }
    if (url.pathname === "/v1/models" && request.method === "GET") {
      return json(response, 200, { object: "list", data: [{ id: "rusa-smoke-model", object: "model", owned_by: "rusa-ai" }] });
    }
    if (url.pathname === "/metrics" && request.method === "GET") {
      if (apiKey !== "metrics-smoke-key") return json(response, 401, { detail: "Invalid metrics API key" });
      response.writeHead(200, { "Content-Type": "text/plain; version=0.0.4" });
      response.end("# HELP rusa_api_requests_total Smoke metric\nrusa_api_requests_total 3\n");
      return;
    }
    if (url.pathname === "/v1/aicode/tasks" && request.method === "GET") {
      const items = url.searchParams.get("status") === "awaiting_approval" ? [approvalTask] : [seededTask];
      return json(response, 200, { items, total: 1, limit: Number(url.searchParams.get("limit") || 25), offset: Number(url.searchParams.get("offset") || 0) });
    }
    if (url.pathname === "/v1/aicode/tasks" && request.method === "POST") {
      if (!identityFor(apiKey) || apiKey === "readonly-key") return json(response, 403, { detail: "task:create permission required" });
      const body = await readJson(request);
      if (!body.objective || !body.repository?.full_name || !Array.isArray(body.acceptance_criteria)) {
        return json(response, 422, { detail: "Invalid AICode task payload" });
      }
      return json(response, 202, { task_id: "00000000-0000-4000-8000-000000000003", tenant_id: "tenant-smoke", status: "queued" });
    }
    const taskMatch = url.pathname.match(/^\/v1\/aicode\/tasks\/([0-9a-f-]+)$/i);
    if (taskMatch && request.method === "GET") {
      const task = taskMatch[1] === approvalTask.task_id ? approvalTask : seededTask;
      return json(response, 200, { ...task, task_id: taskMatch[1] });
    }
    if (taskMatch && request.method === "DELETE") {
      if (apiKey === "readonly-key") return json(response, 403, { detail: "task:cancel permission required" });
      return json(response, 202, { task_id: taskMatch[1], status: "cancelled" });
    }
    const retryMatch = url.pathname.match(/^\/v1\/aicode\/tasks\/([0-9a-f-]+)\/retry$/i);
    if (retryMatch && request.method === "POST") {
      if (apiKey === "readonly-key") return json(response, 403, { detail: "task:retry permission required" });
      if (retryMatch[1] === approvalTask.task_id) {
        return json(response, 409, { detail: "Task is unavailable or not in a retryable state" });
      }
      return json(response, 202, { task_id: retryMatch[1], status: "queued" });
    }
    if (url.pathname === "/v1/chat/completions" && request.method === "POST") {
      if (apiKey === "readonly-key") return json(response, 403, { detail: "Inference permission required" });
      const body = await readJson(request);
      if (body.stream) {
        response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
        response.end('data: {"choices":[{"delta":{"content":"RUSA smoke response"}}]}\n\ndata: [DONE]\n\n');
        return;
      }
      return json(response, 200, { choices: [{ message: { role: "assistant", content: "RUSA smoke response" } }] });
    }
    if (url.pathname === "/v1/validate-task" && request.method === "POST") {
      const body = await readJson(request);
      if (!body.envelope || typeof body.envelope !== "object") return json(response, 422, { detail: "Envelope required" });
      return json(response, 200, { valid: true, errors: [] });
    }
    return json(response, 404, { detail: "Mock backend route not found" });
  } catch (error) {
    json(response, 500, { detail: error instanceof Error ? error.message : "Mock backend error" });
  }
});

async function freePorts(count) {
  const servers = Array.from({ length: count }, () => createServer());
  await Promise.all(servers.map((server) => new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolveListen);
  })));
  const ports = servers.map((server) => {
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("Could not allocate a local port");
    return address.port;
  });
  await Promise.all(servers.map((server) => new Promise((resolveClose, reject) => server.close((error) => error ? reject(error) : resolveClose()))));
  return ports;
}
async function waitForServer(url, child, output) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (child.exitCode !== null) throw new Error("Next.js server exited before becoming ready.\n" + output());
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1000) });
      if (response.ok) return;
    } catch { /* Wait for the standalone server to bind. */ }
    await delay(250);
  }
  throw new Error("Next.js server did not become ready.\n" + output());
}

const [backendPort, appPort] = await freePorts(2);
await new Promise((resolveListen, reject) => {
  backend.once("error", reject);
  backend.listen(backendPort, "127.0.0.1", resolveListen);
});

let appOutput = "";
const app = spawn(process.execPath, [standaloneServer], {
  cwd: standaloneDir,
  env: {
    ...process.env,
    NODE_ENV: "production",
    HOSTNAME: "127.0.0.1",
    PORT: String(appPort),
    RUSA_API_BASE_URL: `http://127.0.0.1:${backendPort}`,
    RUSA_WEB_SESSION_SECRET: "ci-only-session-secret-change-this-value-123456789",
    RUSA_WEB_COOKIE_SECURE: "false",
    RUSA_METRICS_API_KEY: "metrics-smoke-key"
  },
  stdio: ["ignore", "pipe", "pipe"]
});
app.stdout.on("data", (chunk) => { appOutput += chunk.toString(); });
app.stderr.on("data", (chunk) => { appOutput += chunk.toString(); });
const base = `http://127.0.0.1:${appPort}`;
let browser;

try {
  await waitForServer(base, app, () => appOutput);
  const landing = await fetch(base);
  assert.equal(landing.status, 200, "Control Center landing page must render");
  assert.match(await landing.text(), /RUSA AI/, "Landing page must render the RUSA AI shell");

  const unauthenticated = await fetch(base + "/api/tasks");
  assert.equal(unauthenticated.status, 401, "Task API must reject missing sessions");

  const login = await fetch(base + "/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base },
    body: JSON.stringify({ apiKey: "platform-key" })
  });
  assert.equal(login.status, 200, "Platform admin login must succeed");
  const identity = await login.json();
  assert.equal(identity.tenant_id, "tenant-smoke");
  assert.deepEqual(identity.roles, ["platform_admin"]);
  assert.equal(JSON.stringify(identity).includes("platform-key"), false, "Raw API key must never be returned to the browser");
  const setCookie = login.headers.get("set-cookie") || "";
  assert.match(setCookie, /HttpOnly/i, "Session cookie must be HttpOnly");
  assert.match(setCookie, /SameSite=Lax/i, "Session cookie must use SameSite=Lax");
  const cookie = setCookie.split(";")[0];
  assert.ok(cookie.startsWith("rusa_session="), "Encrypted session cookie must be issued");
  assert.equal(cookie.includes("platform-key"), false, "Session cookie must not expose the raw API key");

  const session = await fetch(base + "/api/session", { headers: { Cookie: cookie } });
  assert.equal(session.status, 200, "Session must revalidate against the backend");
  assert.equal((await session.json()).subject, "smoke-admin");

  const taskList = await fetch(base + "/api/tasks?limit=25&offset=0", { headers: { Cookie: cookie } });
  assert.equal(taskList.status, 200, "Tenant task list must be available");
  assert.equal((await taskList.json()).items[0].task_id, seededTask.task_id);

  const approvals = await fetch(base + "/api/tasks?status=awaiting_approval&limit=100&offset=0", { headers: { Cookie: cookie } });
  assert.equal(approvals.status, 200, "Approval queue must be readable");
  assert.equal((await approvals.json()).items[0].status, "awaiting_approval");

  const taskDetail = await fetch(base + "/api/tasks/" + seededTask.task_id, { headers: { Cookie: cookie } });
  assert.equal(taskDetail.status, 200, "Task detail route must be available");
  assert.equal((await taskDetail.json()).task_id, seededTask.task_id);

  const created = await fetch(base + "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base, Cookie: cookie, "Idempotency-Key": "smoke-create-001" },
    body: JSON.stringify({
      objective: "Smoke-test create task",
      repository: { full_name: "StrategyG/RusaAi", base_branch: "main" },
      acceptance_criteria: ["The BFF forwards a valid payload"]
    })
  });
  assert.equal(created.status, 202, "Task creation must forward the backend's accepted response");
  assert.equal((await created.json()).status, "queued");

  const retry = await fetch(base + "/api/tasks/" + seededTask.task_id + "/retry", {
    method: "POST", headers: { Origin: base, Cookie: cookie }
  });
  assert.equal(retry.status, 202, "Failed-task retry route must forward the backend response");

  const approvalRetry = await fetch(base + "/api/tasks/" + approvalTask.task_id + "/retry", {
    method: "POST", headers: { Origin: base, Cookie: cookie }
  });
  assert.equal(approvalRetry.status, 409, "Approval-paused tasks must not be retried before a human decision");

  const cancelled = await fetch(base + "/api/tasks/" + seededTask.task_id, {
    method: "DELETE", headers: { Origin: base, Cookie: cookie }
  });
  assert.equal(cancelled.status, 202, "Task cancellation route must forward the backend response");

  const models = await fetch(base + "/api/models", { headers: { Cookie: cookie } });
  assert.equal(models.status, 200, "Model discovery must be available");
  assert.equal((await models.json()).data[0].id, "rusa-smoke-model");

  const health = await fetch(base + "/api/health");
  assert.equal(health.status, 200, "Health proxy must be available");
  const healthData = await health.json();
  assert.equal(healthData.liveness.ok, true);
  assert.equal(healthData.readiness.ok, true);

  const metrics = await fetch(base + "/api/metrics", { headers: { Cookie: cookie } });
  assert.equal(metrics.status, 200, "Platform admin metrics proxy must be available");
  assert.match((await metrics.json()).text, /rusa_api_requests_total/);
  assert.ok(seen.some((item) => item.path === "/metrics" && item.apiKey === "metrics-smoke-key"), "Metrics proxy must use its server-side operator key");

  const chat = await fetch(base + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base, Cookie: cookie },
    body: JSON.stringify({ model: "rusa-smoke-model", messages: [{ role: "user", content: "Hello" }], stream: true })
  });
  assert.equal(chat.status, 200, "Chat stream must be available");
  assert.match(chat.headers.get("content-type") || "", /text\/event-stream/i);
  assert.match(await chat.text(), /RUSA smoke response/);

  const chatJson = await fetch(base + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base, Cookie: cookie },
    body: JSON.stringify({ messages: [{ role: "user", content: "Hello" }], stream: false })
  });
  assert.equal(chatJson.status, 200, "Non-stream chat must be available");
  assert.equal((await chatJson.json()).choices[0].message.content, "RUSA smoke response");

  const validation = await fetch(base + "/api/validate-task", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base, Cookie: cookie },
    body: JSON.stringify({ envelope: { protocol_version: "1.0", type: "task.create" } })
  });
  assert.equal(validation.status, 200, "A2A validator proxy must be available");
  assert.equal((await validation.json()).valid, true);

  const crossOrigin = await fetch(base + "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: "https://attacker.example", Cookie: cookie },
    body: JSON.stringify({ objective: "must be rejected" })
  });
  assert.equal(crossOrigin.status, 403, "Mutating BFF routes must reject cross-origin requests");

  const readOnlyLogin = await fetch(base + "/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base },
    body: JSON.stringify({ apiKey: "readonly-key" })
  });
  assert.equal(readOnlyLogin.status, 200);
  const readOnlyCookie = (readOnlyLogin.headers.get("set-cookie") || "").split(";")[0];
  const deniedMetrics = await fetch(base + "/api/metrics", { headers: { Cookie: readOnlyCookie } });
  assert.equal(deniedMetrics.status, 403, "Read-only users must not access operator metrics");

  const deniedCreate = await fetch(base + "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base, Cookie: readOnlyCookie, "Idempotency-Key": "smoke-readonly-001" },
    body: JSON.stringify({
      objective: "Read-only user must not create tasks",
      repository: { full_name: "StrategyG/RusaAi", base_branch: "main" },
      acceptance_criteria: ["Backend RBAC is authoritative"]
    })
  });
  assert.equal(deniedCreate.status, 403, "Backend task RBAC denial must pass through the BFF");

  const deniedRetry = await fetch(base + "/api/tasks/" + seededTask.task_id + "/retry", {
    method: "POST", headers: { Origin: base, Cookie: readOnlyCookie }
  });
  assert.equal(deniedRetry.status, 403, "Read-only users must not retry tasks");

  const deniedCancel = await fetch(base + "/api/tasks/" + seededTask.task_id, {
    method: "DELETE", headers: { Origin: base, Cookie: readOnlyCookie }
  });
  assert.equal(deniedCancel.status, 403, "Read-only users must not cancel tasks");

  const deniedChat = await fetch(base + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base, Cookie: readOnlyCookie },
    body: JSON.stringify({ messages: [{ role: "user", content: "must be denied" }], stream: false })
  });
  assert.equal(deniedChat.status, 403, "Read-only users must not use inference");

  const logout = await fetch(base + "/api/session", { method: "DELETE", headers: { Origin: base, Cookie: cookie } });
  assert.equal(logout.status, 200, "Logout must clear the session cookie");
  assert.match(logout.headers.get("set-cookie") || "", /Max-Age=0/i);

  const expectedPaths = [
    "/v1/auth/session", "/v1/aicode/tasks", "/v1/models",
    "/v1/chat/completions", "/v1/validate-task", "/health",
    "/health/ready", "/metrics"
  ];
  for (const path of expectedPaths) {
    assert.ok(seen.some((item) => item.path.startsWith(path)), `Expected backend integration call: ${path}`);
  }

  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await page.getByLabel("RUSA API Key").fill("platform-key");
  await page.getByRole("button", { name: "Connect to RUSA AI" }).click();
  await page.locator(".rc-page-head h1").waitFor({ state: "visible" });
  assert.equal((await page.locator(".rc-page-head h1").innerText()).trim(), "Command Center");

  const navigation = [
    ["Orchestrator", "Orchestrator"],
    ["AICode Tasks", "AICode Tasks"],
    ["Approvals", "Approval Center"],
    ["Agent Workspace", "Agent Workspace"],
    ["Agentic DevOps", "Agentic DevOps"],
    ["AI Chat", "RUSA AI Chat"],
    ["Memory & Knowledge", "Memory & Knowledge"],
    ["Repository & Artifacts", "Repository & Artifacts"],
    ["Evaluation & Reports", "Evaluation & Reporting"],
    ["Governance", "Governance & Policy"],
    ["Observability", "Observability"],
    ["Product Pillars", "RUSA AI Product Pillars"],
    ["Developer Tools", "Developer Tools"],
    ["Command Center", "Command Center"]
  ];
  for (const [label, title] of navigation) {
    await page.locator(".rc-nav-item").filter({ hasText: label }).click();
    await page.locator(".rc-page-head h1").filter({ hasText: title }).waitFor({ state: "visible" });
    assert.equal((await page.locator(".rc-page-head h1").innerText()).trim(), title, `Navigation must open ${title}`);
  }

  await page.locator(".rc-nav-item").filter({ hasText: "AICode Tasks" }).click();
  await page.getByRole("button", { name: /New AICode Task/ }).click();
  const taskModal = page.locator(".rc-modal-wide");
  await taskModal.waitFor({ state: "visible" });
  await taskModal.locator("label").filter({ hasText: "Objective" }).locator("textarea").fill("Browser smoke creates a task");
  await taskModal.locator("label").filter({ hasText: "Repository" }).locator("input").fill("StrategyG/RusaAi");
  await taskModal.locator("label").filter({ hasText: "Base branch" }).locator("input").fill("main");
  await taskModal.locator("label").filter({ hasText: "Acceptance criteria" }).locator("textarea").fill("The browser smoke task is accepted");
  await taskModal.getByRole("button", { name: /Submit task/ }).click();
  await page.locator(".rc-task-modal").waitFor({ state: "visible" });
  assert.match(await page.locator(".rc-task-modal").innerText(), /Smoke-test task detail/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator(".rc-task-modal").getByRole("button", { name: /Retry failed task/ }).click();
  await page.locator(".rc-alert-success").filter({ hasText: "Task diterima kembali oleh antrean" }).waitFor({ state: "visible" });
  await page.locator(".rc-task-modal button[aria-label='Close']").click();

  await page.locator(".rc-nav-item").filter({ hasText: "Agent Workspace" }).click();
  await page.locator(".rc-agent-button").filter({ hasText: "Supreme Orchestrator" }).click();
  await page.locator(".rc-agent-modal").waitFor({ state: "visible" });
  await page.locator(".rc-agent-modal button[aria-label='Close']").click();

  await page.locator(".rc-nav-item").filter({ hasText: "Agentic DevOps" }).click();
  await page.locator(".rc-stage-item").filter({ hasText: "Code" }).click();
  assert.equal((await page.locator(".rc-stage-detail h2").innerText()).trim(), "Code Agent");

  await page.locator(".rc-nav-item").filter({ hasText: "Approvals" }).click();
  assert.equal(await page.getByRole("button", { name: /Approve|Reject/i }).count(), 0, "Approval page must not fabricate decision actions");
  const approvalRow = page.locator(".rc-approval-row").filter({ hasText: "Smoke-test task detail" });
  await approvalRow.getByRole("button", { name: /Inspect task/ }).click();
  await page.locator(".rc-task-modal").waitFor({ state: "visible" });
  assert.equal(await page.locator(".rc-task-modal").getByRole("button", { name: /Retry failed task/ }).count(), 0, "Approval-paused task detail must not expose retry");
  await page.locator(".rc-task-modal button[aria-label='Close']").click();

  await page.locator(".rc-nav-item").filter({ hasText: "AI Chat" }).click();
  await page.locator(".rc-chat-compose textarea").fill("Hello from browser smoke");
  await page.locator(".rc-chat-compose button[type='submit']").click();
  await page.locator(".rc-chat-line.assistant").last().locator("p").filter({ hasText: "RUSA smoke response" }).waitFor({ state: "visible" });

  await page.locator(".rc-nav-item").filter({ hasText: "Developer Tools" }).click();
  await page.getByRole("button", { name: /Validate envelope/ }).click();
  await page.locator(".rc-validation-result.valid").waitFor({ state: "visible" });

  await page.locator(".rc-nav-item").filter({ hasText: "Observability" }).click();
  await page.locator(".rc-metrics-meta").filter({ hasText: "Fetched" }).waitFor({ state: "visible" });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Toggle navigation" }).click();
  assert.equal(await page.locator(".rc-sidebar").evaluate((element) => element.classList.contains("rc-sidebar-open")), true);
  await page.locator(".rc-nav-item").filter({ hasText: "Command Center" }).click();
  assert.equal(await page.locator(".rc-sidebar").evaluate((element) => element.classList.contains("rc-sidebar-open")), false);
  await page.setViewportSize({ width: 1280, height: 900 });

  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("heading", { name: "Masuk ke RUSA AI" }).waitFor({ state: "visible" });

  await page.getByLabel("RUSA API Key").fill("developer-key");
  await page.getByRole("button", { name: "Connect to RUSA AI" }).click();
  await page.locator(".rc-page-head h1").filter({ hasText: "Command Center" }).waitFor({ state: "visible" });
  assert.equal(await page.getByRole("button", { name: /New AICode Task/ }).count(), 1, "Developers may create tasks");
  await page.locator(".rc-nav-item").filter({ hasText: "AICode Tasks" }).click();
  await page.locator(".rc-table-link").filter({ hasText: "Smoke-test task detail" }).click();
  await page.locator(".rc-task-modal").waitFor({ state: "visible" });
  assert.equal(await page.locator(".rc-task-modal").getByRole("button", { name: /Retry failed task|Cancel task/ }).count(), 0, "Developers must not receive admin task controls");
  await page.locator(".rc-task-modal button[aria-label='Close']").click();
  await page.locator(".rc-nav-item").filter({ hasText: "AI Chat" }).click();
  assert.equal(await page.locator(".rc-chat-compose textarea").isDisabled(), false, "Developers may use inference");

  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("heading", { name: "Masuk ke RUSA AI" }).waitFor({ state: "visible" });
  await page.getByLabel("RUSA API Key").fill("readonly-key");
  await page.getByRole("button", { name: "Connect to RUSA AI" }).click();
  await page.locator(".rc-page-head h1").filter({ hasText: "Command Center" }).waitFor({ state: "visible" });
  assert.equal(await page.getByRole("button", { name: /New AICode Task/ }).count(), 0, "Read-only users must not create tasks");
  await page.locator(".rc-nav-item").filter({ hasText: "AI Chat" }).click();
  assert.equal(await page.locator(".rc-chat-compose textarea").isDisabled(), true, "Read-only users must not use inference");
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("heading", { name: "Masuk ke RUSA AI" }).waitFor({ state: "visible" });

  await browser.close();
  browser = undefined;

  process.stdout.write("RUSA AI web functional smoke: PASS (BFF contracts + platform_admin/developer/read_only UI role matrix, failed-only retry, approval safety, navigation, agent contracts, DevOps stages, chat, validator, responsive navigation, logout).\\n");
} catch (error) {
  process.stderr.write("RUSA AI web functional smoke: FAIL\n");
  process.stderr.write((error instanceof Error ? error.stack : String(error)) + "\n");
  process.stderr.write(appOutput + "\n");
  process.exitCode = 1;
} finally {
  if (browser) await browser.close().catch(() => {});
  app.kill("SIGTERM");
  await new Promise((resolveClose) => backend.close(resolveClose));
}
