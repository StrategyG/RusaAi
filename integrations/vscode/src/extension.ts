import * as vscode from "vscode";

const API_KEY_SECRET = "rusaai.apiKey";
const API_BASE_SETTING = "rusaai.apiBaseUrl";
const MODEL_SETTING = "rusaai.model";

function configuredEndpoint(value: string): string {
  const endpoint = new URL(value);
  const loopback = endpoint.hostname === "localhost" || endpoint.hostname === "127.0.0.1" || endpoint.hostname === "::1";
  if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    throw new Error("RUSA AI endpoint must not contain credentials, query parameters or fragments.");
  }
  if (endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && loopback)) {
    throw new Error("Use HTTPS for remote endpoints or HTTP only for localhost.");
  }
  return endpoint.toString().replace(/\/+$/, "");
}

async function configureConnection(context: vscode.ExtensionContext): Promise<void> {
  const configuration = vscode.workspace.getConfiguration("rusaai");
  const current = configuration.get<string>("apiBaseUrl", "http://127.0.0.1:8000/v1");
  const enteredEndpoint = await vscode.window.showInputBox({
    title: "Connect RUSA AI",
    prompt: "Local API base URL",
    value: current,
    ignoreFocusOut: true
  });
  if (enteredEndpoint === undefined) return;

  let endpoint: string;
  try {
    endpoint = configuredEndpoint(enteredEndpoint.trim());
  } catch (error) {
    void vscode.window.showErrorMessage(error instanceof Error ? error.message : "Invalid RUSA AI endpoint.");
    return;
  }

  const apiKey = await vscode.window.showInputBox({
    title: "RUSA AI API Key",
    prompt: "Paste the key shown by your local RUSA AI first-run page. It is stored in VS Code SecretStorage.",
    password: true,
    ignoreFocusOut: true
  });
  if (apiKey === undefined) return;
  if (!apiKey.startsWith("rusa_sk_") || apiKey.length < 32) {
    void vscode.window.showErrorMessage("The supplied value does not look like a RUSA AI local API key.");
    return;
  }

  await configuration.update(API_BASE_SETTING, endpoint, vscode.ConfigurationTarget.Global);
  await context.secrets.store(API_KEY_SECRET, apiKey);
  try {
    const response = await fetch(endpoint + "/models", {
      headers: { "Authorization": "Bearer " + apiKey },
      signal: AbortSignal.timeout(5000)
    });
    if (response.ok) {
      const payload = await response.json() as { data?: Array<{ id?: string }> };
      const modelId = payload.data?.find((item) => typeof item.id === "string" && item.id)?.id;
      if (modelId) {
        await configuration.update(MODEL_SETTING, modelId, vscode.ConfigurationTarget.Global);
      }
    }
  } catch {
    // Keep the saved connection; the user can set the model alias when the runtime is ready.
  }
  void vscode.window.showInformationMessage("RUSA AI connection saved securely in VS Code.");
}

async function answerWithRusaAI(
  request: vscode.ChatRequest,
  _context: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
  extensionContext: vscode.ExtensionContext
): Promise<vscode.ChatResult> {
  const configuration = vscode.workspace.getConfiguration("rusaai");
  let baseUrl: string;
  try {
    baseUrl = configuredEndpoint(configuration.get<string>("apiBaseUrl", "http://127.0.0.1:8000/v1"));
  } catch {
    stream.markdown("The configured RUSA AI endpoint is invalid. Run **RUSA AI: Configure Local API** to correct it.");
    return {};
  }
  const model = configuration.get<string>("model", "default");
  const apiKey = await extensionContext.secrets.get(API_KEY_SECRET);
  if (!apiKey) {
    stream.markdown("Connect this editor first: run **RUSA AI: Configure Local API** and enter the API key from the local RUSA AI setup page.");
    return {};
  }

  const controller = new AbortController();
  const cancellation = token.onCancellationRequested(() => controller.abort());
  try {
    if (token.isCancellationRequested) return {};
    const response = await fetch(baseUrl + "/chat/completions", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + apiKey,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        model,
        messages: [{ role: "user", content: request.prompt }],
        temperature: 0.2,
        max_tokens: 2048,
        stream: false
      }),
      signal: controller.signal
    });
    if (!response.ok) {
      stream.markdown("RUSA AI request failed (" + response.status + "). Check the local service and API key.");
      return {};
    }
    const payload = await response.json() as {
      choices?: Array<{ message?: { content?: string } }>
    };
    const answer = payload.choices?.[0]?.message?.content;
    if (typeof answer !== "string" || !answer) {
      stream.markdown("RUSA AI returned an empty or unsupported response.");
      return {};
    }
    stream.markdown(answer);
  } catch {
    if (token.isCancellationRequested) return {};
    stream.markdown("Could not reach the configured RUSA AI endpoint. Confirm the local service is running.");
  } finally {
    cancellation.dispose();
  }
  return {};
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("rusaai.configureConnection", () => configureConnection(context))
  );
  const participant = vscode.chat.createChatParticipant(
    "rusaai.chat",
    (request, chatContext, stream, token) =>
      answerWithRusaAI(request, chatContext, stream, token, context)
  );
  participant.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "rusaai.svg");
  context.subscriptions.push(participant);
}

export function deactivate(): void {}
