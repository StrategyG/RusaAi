const $ = (selector) => document.querySelector(selector);
const state = { chatId: null, busy: false, chats: [], docs: [], tools: [] };

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 2400);
}
async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || "Permintaan gagal.");
  return data;
}
function openDialog(id) { const dialog = document.getElementById(id); if (dialog && !dialog.open) dialog.showModal(); }
function closeDialog(id) { const dialog = document.getElementById(id); if (dialog && dialog.open) dialog.close(); }
function setModelState(ready, name) {
  ["#modelDot", "#topModelDot"].forEach((selector) => {
    const node = $(selector);
    node.classList.toggle("ready", ready);
    node.classList.toggle("error", !ready);
  });
  $("#modelStatus").textContent = ready ? "Model siap digunakan" : "Model belum aktif";
  $("#modelName").textContent = name || "Qwen local";
  $("#topModelLabel").textContent = ready ? "Qwen · Lokal" : "Qwen · Offline";
}
async function refreshStatus() {
  try {
    const data = await api("/api/status");
    setModelState(data.model.ready, data.model.name);
    $("#docCount").textContent = data.rag.documents;
  } catch {
    setModelState(false, "Qwen local");
  }
}
function renderChatList() {
  const root = $("#chatList");
  root.replaceChildren();
  if (!state.chats.length) {
    const empty = document.createElement("div");
    empty.className = "side-empty";
    empty.textContent = "Belum ada percakapan";
    root.append(empty);
    return;
  }
  state.chats.forEach((chat) => {
    const row = document.createElement("div");
    row.className = "chat-item" + (chat.id === state.chatId ? " active" : "");
    const title = document.createElement("span");
    title.textContent = chat.title || "Percakapan baru";
    title.title = title.textContent;
    title.addEventListener("click", () => loadChat(chat.id));
    const remove = document.createElement("button");
    remove.className = "chat-delete";
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Hapus percakapan");
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!confirm("Hapus percakapan ini?")) return;
      try {
        await api("/api/chats/" + encodeURIComponent(chat.id), { method: "DELETE" });
        if (state.chatId === chat.id) await newChat();
        await refreshChats();
      } catch (error) { toast(error.message); }
    });
    row.append(title, remove);
    root.append(row);
  });
}
async function refreshChats() {
  const data = await api("/api/chats");
  state.chats = data.chats || [];
  renderChatList();
}
function clearMessages() {
  $("#messages").replaceChildren();
  $("#welcome").classList.remove("hidden");
}
function addMessage(role, content, sources = []) {
  $("#welcome").classList.add("hidden");
  const row = document.createElement("article");
  row.className = "message " + role;
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.textContent = role === "assistant" ? "H" : "U";
  const body = document.createElement("div");
  body.className = "message-content";
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "assistant" ? "HYBRIDA" : "KAMU";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = content || "";
  body.append(label, bubble);
  if (sources.length) {
    const sourceRow = document.createElement("div");
    sourceRow.className = "message-sources";
    sources.forEach((source) => {
      const chip = document.createElement("span");
      chip.className = "source-chip";
      chip.textContent = "▤ " + source.title;
      chip.title = source.source_uri;
      sourceRow.append(chip);
    });
    body.append(sourceRow);
  }
  if (role === "assistant" && content) {
    const actions = document.createElement("div");
    actions.className = "message-actions";
    const copy = document.createElement("button");
    copy.className = "message-action";
    copy.textContent = "Salin jawaban";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(bubble.textContent); toast("Jawaban disalin."); }
      catch { toast("Clipboard tidak tersedia."); }
    });
    actions.append(copy);
    body.append(actions);
  }
  row.append(avatar, body);
  $("#messages").append(row);
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
  return bubble;
}
async function newChat() {
  try {
    const data = await api("/api/chats", { method: "POST" });
    state.chatId = data.id;
    clearMessages();
    await refreshChats();
    $("#messageInput").focus();
  } catch (error) { toast(error.message); }
}
async function loadChat(id) {
  try {
    const data = await api("/api/chats/" + encodeURIComponent(id));
    state.chatId = id;
    clearMessages();
    (data.messages || []).forEach((message) => {
      if (message.role !== "system") addMessage(message.role, message.content);
    });
    renderChatList();
    closeSidebar();
  } catch (error) { toast(error.message); }
}
function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#scrim").classList.remove("show");
}
function openSidebar() {
  $("#sidebar").classList.add("open");
  $("#scrim").classList.add("show");
}
function showTyping(show) { $("#typing").classList.toggle("hidden", !show); }
function parseSseBlock(block, handlers) {
  let eventName = "message";
  const dataLines = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return;
  let data;
  try { data = JSON.parse(dataLines.join("\n")); } catch { return; }
  if (handlers[eventName]) handlers[eventName](data);
}
async function sendMessage(text) {
  const message = (text || $("#messageInput").value).trim();
  if (!message || state.busy) return;
  if (!state.chatId) await newChat();
  if (!state.chatId) return;
  state.busy = true;
  $("#sendBtn").disabled = true;
  $("#messageInput").value = "";
  $("#messageInput").style.height = "auto";
  addMessage("user", message);
  const bubble = addMessage("assistant", "");
  showTyping(true);
  let sources = [];
  let assistantText = "";
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: state.chatId,
        message,
        use_rag: $("#ragToggle").checked,
        temperature: 0.6,
      })
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || "Model lokal tidak dapat dihubungi.");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const handlers = {
      meta: (data) => {
        state.chatId = data.chat_id || state.chatId;
        sources = data.sources || [];
      },
      delta: (data) => {
        assistantText += data.content || "";
        bubble.textContent = assistantText;
        $("#conversation").scrollTop = $("#conversation").scrollHeight;
      },
      error: (data) => { throw new Error(data.message || "Inference error."); },
      done: async () => { showTyping(false); }
    };
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      blocks.forEach((block) => parseSseBlock(block, handlers));
    }
    if (buffer.trim()) parseSseBlock(buffer, handlers);
    if (assistantText && sources.length) {
      const row = bubble.closest(".message");
      const sourceRow = document.createElement("div");
      sourceRow.className = "message-sources";
      sources.forEach((source) => {
        const chip = document.createElement("span");
        chip.className = "source-chip";
        chip.textContent = "▤ " + source.title;
        chip.title = source.source_uri;
        sourceRow.append(chip);
      });
      bubble.after(sourceRow);
    }
    if (!assistantText) bubble.textContent = "Model tidak menghasilkan jawaban. Coba lagi.";
    await refreshChats();
  } catch (error) {
    bubble.textContent = error.message || "Terjadi kesalahan.";
    toast(error.message || "Terjadi kesalahan.");
  } finally {
    showTyping(false);
    state.busy = false;
    $("#sendBtn").disabled = false;
    $("#messageInput").focus();
  }
}
async function refreshDocuments() {
  try {
    const data = await api("/api/documents");
    state.docs = data.documents || [];
    $("#docCount").textContent = state.docs.length;
    $("#docTotal").textContent = state.docs.length;
    const root = $("#documentList");
    root.replaceChildren();
    if (!state.docs.length) {
      const empty = document.createElement("div");
      empty.className = "side-empty";
      empty.textContent = "Belum ada dokumen. Tambahkan file untuk mengaktifkan knowledge lokal.";
      root.append(empty);
      return;
    }
    state.docs.forEach((doc) => {
      const row = document.createElement("div");
      row.className = "document-row";
      const icon = document.createElement("div");
      icon.className = "document-icon";
      icon.textContent = "▤";
      const main = document.createElement("div");
      main.className = "document-main";
      const title = document.createElement("b");
      title.textContent = doc.title;
      const meta = document.createElement("small");
      meta.textContent = doc.collection_name + " · " + doc.chunk_count + " potongan";
      main.append(title, meta);
      const remove = document.createElement("button");
      remove.className = "delete-doc";
      remove.textContent = "×";
      remove.title = "Hapus dokumen";
      remove.addEventListener("click", async () => {
        if (!confirm("Hapus dokumen dari knowledge lokal?")) return;
        try {
          await api("/api/documents/" + encodeURIComponent(doc.id), { method: "DELETE" });
          await refreshDocuments();
          await refreshStatus();
          toast("Dokumen dihapus.");
        } catch (error) { toast(error.message); }
      });
      row.append(icon, main, remove);
      root.append(row);
    });
  } catch (error) { toast(error.message); }
}
async function uploadFile(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  form.append("collection", "default");
  try {
    const response = await fetch("/api/documents", { method: "POST", body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Upload gagal.");
    await refreshDocuments();
    await refreshStatus();
    toast(data.duplicate ? "Dokumen sudah ada." : "Dokumen berhasil diindeks.");
  } catch (error) { toast(error.message); }
  $("#fileInput").value = "";
}
async function refreshTools() {
  try {
    const data = await api("/api/mcp/tools");
    state.tools = data.tools || [];
    const root = $("#toolList");
    root.replaceChildren();
    state.tools.forEach((tool) => {
      const row = document.createElement("div");
      row.className = "tool-row";
      const icon = document.createElement("div");
      icon.className = "tool-icon";
      icon.textContent = "⌘";
      const main = document.createElement("div");
      main.className = "tool-main";
      const title = document.createElement("b");
      title.textContent = tool.name;
      const desc = document.createElement("small");
      desc.textContent = tool.description;
      main.append(title, desc);
      const run = document.createElement("button");
      run.className = "tool-run";
      run.textContent = "Jalankan";
      run.addEventListener("click", () => runTool(tool.name));
      row.append(icon, main, run);
      root.append(row);
    });
  } catch (error) { toast(error.message); }
}
async function runTool(name) {
  let argumentsValue = {};
  if (name === "search_knowledge") {
    const query = prompt("Apa yang ingin dicari di dokumen lokal?");
    if (!query) return;
    argumentsValue = { query, limit: 5 };
  } else if (name === "get_document") {
    const id = prompt("Masukkan document_id dari daftar dokumen:");
    if (!id) return;
    argumentsValue = { document_id: id };
  } else if (name === "create_note") {
    const title = prompt("Judul catatan:");
    if (!title) return;
    const content = prompt("Isi catatan:");
    if (!content) return;
    argumentsValue = { title, content };
  } else if (name === "calculate") {
    const expression = prompt("Masukkan operasi matematika (contoh: (125*4)/5):");
    if (!expression) return;
    argumentsValue = { expression };
  }
  try {
    const data = await api("/api/mcp/call", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, arguments: argumentsValue })
    });
    $("#toolResult").classList.remove("hidden");
    $("#toolOutput").textContent = JSON.stringify(data.result, null, 2);
    if (name === "create_note") await refreshDocuments();
  } catch (error) { toast(error.message); }
}
function bindEvents() {
  $("#newChat").addEventListener("click", newChat);
  $("#menuBtn").addEventListener("click", openSidebar);
  $("#closeSidebar").addEventListener("click", closeSidebar);
  $("#scrim").addEventListener("click", closeSidebar);
  $("#openDocs").addEventListener("click", async () => { await refreshDocuments(); openDialog("docsDialog"); closeSidebar(); });
  $("#topDocs").addEventListener("click", async () => { await refreshDocuments(); openDialog("docsDialog"); });
  $("#docsInline").addEventListener("click", async () => { await refreshDocuments(); openDialog("docsDialog"); });
  $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
  $("#chooseFile").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", (event) => uploadFile(event.target.files[0]));
  $("#openTools").addEventListener("click", async () => { await refreshTools(); openDialog("toolsDialog"); closeSidebar(); });
  $("#toolsInline").addEventListener("click", async () => { await refreshTools(); openDialog("toolsDialog"); });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => closeDialog(button.dataset.close)));
  document.querySelectorAll(".suggestion").forEach((button) => button.addEventListener("click", () => sendMessage(button.dataset.prompt)));
  $("#chatForm").addEventListener("submit", (event) => { event.preventDefault(); sendMessage(); });
  $("#messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); }
  });
  $("#messageInput").addEventListener("input", (event) => {
    event.target.style.height = "auto";
    event.target.style.height = Math.min(event.target.scrollHeight, 150) + "px";
  });
}
async function init() {
  bindEvents();
  try { await refreshChats(); } catch (error) { toast(error.message); }
  await refreshStatus();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  window.setInterval(refreshStatus, 15000);
}
document.addEventListener("DOMContentLoaded", init);
