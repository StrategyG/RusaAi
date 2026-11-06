import { useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { AlertCircle, ArrowUp, Bot, Check, Copy, MessageSquareText, Sparkles, UserRound } from "lucide-react";
import { api } from "../api";

type Message = { role: "user" | "assistant"; content: string; id: string };

export function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [modelName, setModelName] = useState("rusa-ai-v1");
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const [modelStatusError, setModelStatusError] = useState("");

  useEffect(() => {
    let active = true;
    api<{ model: { name: string; loaded: boolean } }>("/v1/ui/overview")
      .then((overview) => {
        if (active) {
          if (overview.model?.name) setModelName(overview.model.name);
          setModelReady(Boolean(overview.model?.loaded));
          setModelStatusError("");
        }
      })
      .catch((err) => {
        if (active) {
          setModelReady(false);
          setModelStatusError(err instanceof Error ? err.message : "Unable to verify model readiness");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  async function sendMessage(event?: FormEvent) {
    event?.preventDefault();
    const content = prompt.trim();
    if (!content || busy || modelReady !== true) return;
    const nextMessages = [...messages, { role: "user" as const, content, id: crypto.randomUUID() }];
    setMessages(nextMessages);
    setPrompt("");
    setError("");
    setBusy(true);
    try {
      const result = await api<{ choices: { message: { content: string } }[] }>("/v1/chat/completions", {
        method: "POST",
        body: JSON.stringify({
          model: modelName,
          messages: nextMessages.slice(-128).map(({ role, content: text }) => ({ role, content: text })),
          temperature: 0.7,
          max_tokens: 1024,
          stream: false,
        }),
      });
      const answer = result.choices?.[0]?.message?.content;
      if (!answer) throw new Error("The model returned an empty response");
      setMessages((current) => [...current, { role: "assistant", content: answer, id: crypto.randomUUID() }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The request could not be completed");
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage();
    }
  }

  async function copyMessage(message: Message) {
    await navigator.clipboard.writeText(message.content);
    setCopied(message.id);
    window.setTimeout(() => setCopied(""), 1500);
  }

  return (
    <div className="chat-layout">
      <div className="page-heading chat-heading"><div><div className="eyebrow muted">AI WORKSPACE</div><h1>Ask RUSA-AI</h1><p>Work through questions, ideas, and complex tasks with your AI assistant.</p></div><span className="model-tag"><span className={"status-dot" + (modelReady === false ? " offline" : modelReady === null ? " checking" : "")} /> {modelName} · {modelReady === true ? "Online" : modelReady === false ? "Offline" : "Checking"}</span></div>
      <section className="chat-panel">
        {modelReady !== true && <div className="notice-card warning chat-model-warning"><AlertCircle size={18} /><div><strong>{modelReady === null ? "Checking inference availability" : "Inference model is unavailable"}</strong><p>{modelStatusError || (modelReady === null ? "Checking the API and model status. Chat will be enabled when the model is ready." : "The API is reachable, but the configured model is not loaded. Chat is paused until the administrator restores model readiness.")}</p></div></div>}
        <div className="chat-messages">
          {!messages.length && <div className="chat-welcome"><div className="welcome-orb"><Sparkles size={27} /></div><h2>What would you like to work on?</h2><p>Start with a question, paste context, or ask RUSA-AI to help you think through a problem.</p><div className="prompt-suggestions"><button onClick={() => setPrompt("Help me break down a complex project into clear milestones.")}>Plan a project <ArrowUp size={14} /></button><button onClick={() => setPrompt("Review this idea and identify its risks and opportunities.")}>Review an idea <ArrowUp size={14} /></button><button onClick={() => setPrompt("Explain this technical concept in simple terms.")}>Explain a concept <ArrowUp size={14} /></button></div></div>}
          {messages.map((message) => <div className={"message-row " + message.role} key={message.id}><div className="message-avatar">{message.role === "assistant" ? <Bot size={17} /> : <UserRound size={17} />}</div><div className="message-content"><div className="message-label">{message.role === "assistant" ? "RUSA-AI" : "You"}</div><div className="message-bubble">{message.content}</div>{message.role === "assistant" && <button className="copy-button" onClick={() => void copyMessage(message)}>{copied === message.id ? <Check size={13} /> : <Copy size={13} />}{copied === message.id ? "Copied" : "Copy"}</button>}</div></div>)}
          {busy && <div className="message-row assistant"><div className="message-avatar"><Bot size={17} /></div><div className="message-content"><div className="message-label">RUSA-AI</div><div className="typing-indicator"><i /><i /><i /></div></div></div>}
        </div>
        {error && <div className="chat-error">{error}</div>}
        <form className="composer" onSubmit={(event) => void sendMessage(event)}><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={onKeyDown} placeholder={modelReady === true ? "Message RUSA-AI…" : modelReady === null ? "Checking model availability…" : "Chat is unavailable until the model is online…"} rows={2} maxLength={32768} disabled={busy || modelReady !== true} /><div className="composer-footer"><span>Enter to send · Shift + Enter for a new line</span><button className="send-button" disabled={!prompt.trim() || busy || modelReady !== true} aria-label="Send message"><ArrowUp size={19} /></button></div></form>
      </section>
      <div className="chat-disclaimer"><MessageSquareText size={14} /> Responses are generated by the configured RUSA-AI inference model. Verify important information.</div>
    </div>
  );
}
