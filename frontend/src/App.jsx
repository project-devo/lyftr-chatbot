import { useEffect, useRef, useState } from "react";
import { API_URL, sendChat, waitForHealth } from "./api";

// New session on every page load (1-128 chars).
const newSessionId = () => "s-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

const SIMULATIONS = [
  { value: "", label: "Off" },
  { value: "key_1=429", label: "key_1 → 429 (fail over to key_2)" },
  { value: "key_1=500", label: "key_1 → 500 (fail over to key_2)" },
  { value: "*=429", label: "all keys → 429 (expect 503)" },
];

function Meta({ meta }) {
  const cached = meta.resolution_source === "LLM_CACHE";
  return (
    <div className="meta">
      <span data-testid="meta-source" className={`badge ${cached ? "cache" : "live"}`}>
        {meta.resolution_source}
      </span>
      <span className="chip" title="Model">
        <span data-testid="meta-model">{meta.model}</span>
      </span>
      <span className="chip" title="Server-measured latency">
        <span data-testid="meta-latency">{meta.latency_ms} ms</span>
      </span>
      <span className="chip" title="Input (prompt) tokens for this request">
        in <b data-testid="meta-input-tokens">{meta.input_tokens}</b>
      </span>
      <span className="chip" title="Output (completion) tokens for this reply">
        out <b data-testid="meta-output-tokens">{meta.output_tokens}</b>
      </span>
      <span className="chip" title="API key that answered (none for cache hits)">
        key <b data-testid="meta-key-id">{meta.key_id ?? "—"}</b>
      </span>
      <span className={`chip ${meta.fallback_attempts > 0 ? "warn" : ""}`} title="Keys that failed before this one">
        fallbacks <b data-testid="meta-fallback-attempts">{meta.fallback_attempts}</b>
      </span>
    </div>
  );
}

export default function App() {
  const [sessionId] = useState(newSessionId);
  const [messages, setMessages] = useState([]); // { role: "user" | "assistant" | "error", content, metadata? }
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [pending, setPending] = useState(false);
  const [server, setServer] = useState("checking"); // checking | waking | ready | down
  const endRef = useRef(null);

  useEffect(() => {
    let active = true;
    waitForHealth(() => active && setServer((s) => (s === "checking" ? "waking" : s))).then(
      (ok) => active && setServer(ok ? "ready" : "down"),
    );
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  async function send() {
    const text = input;
    if (!text || pending) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setPending(true);
    try {
      const body = { session_id: sessionId, message: text };
      if (systemPrompt.trim()) body.system_prompt = systemPrompt.trim();
      const data = await sendChat(body, simulate);
      setMessages((m) => [...m, { role: "assistant", content: data.reply, metadata: data.metadata }]);
      setServer("ready");
    } catch (err) {
      setMessages((m) => [...m, { role: "error", content: err.message || String(err) }]);
    } finally {
      setPending(false);
    }
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>LLM Chatbot</h1>
          <p className="sub">
            Key failover · response caching · per-request tokens — session <code>{sessionId}</code>
          </p>
        </div>
        <button className="ghost" onClick={() => window.location.reload()} title="Start a new session">
          New chat
        </button>
      </header>

      <section className="settings">
        <label className="field grow">
          <span>System prompt (optional, applies to this session)</span>
          <textarea
            data-testid="system-prompt-input"
            rows={2}
            placeholder="e.g. You are a concise assistant that answers like a pirate."
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
          />
        </label>
        <label className="field">
          <span>Failure simulation (demo)</span>
          <select value={simulate} onChange={(e) => setSimulate(e.target.value)}>
            {SIMULATIONS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </section>

      {server === "waking" && (
        <div className="banner" role="status">
          <span className="spinner" /> Waking up the server… free hosting sleeps when idle, this can take up to a minute.
        </div>
      )}
      {server === "down" && (
        <div className="banner error" role="alert">
          Can't reach the server at {API_URL}. You can still try sending; requests are retried on network errors.
        </div>
      )}

      <main className="chat" aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <p>Ask anything. Every reply shows where it came from, the model, latency and tokens for that request.</p>
            <p className="hint">Tip: send the same first message in a new chat to see an LLM_CACHE hit.</p>
          </div>
        )}
        {messages.map((m, i) =>
          m.role === "assistant" ? (
            <div key={i} className="row left">
              <div className="bubble assistant" data-testid="assistant-message">
                <div className="text">{m.content}</div>
                <Meta meta={m.metadata} />
              </div>
            </div>
          ) : m.role === "error" ? (
            <div key={i} className="row left">
              <div className="bubble error" role="alert" data-testid="error-message">
                {m.content}
              </div>
            </div>
          ) : (
            <div key={i} className="row right">
              <div className="bubble user">{m.content}</div>
            </div>
          ),
        )}
        {pending && (
          <div className="row left">
            <div className="bubble assistant typing" aria-label="Assistant is typing">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
        <div ref={endRef} />
      </main>

      <footer className="composer">
        <textarea
          data-testid="chat-input"
          rows={1}
          placeholder="Type a message… (Enter to send, Shift+Enter for a new line)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          autoFocus
        />
        <button data-testid="send-button" onClick={send} disabled={pending || !input}>
          {pending ? "Sending…" : "Send"}
        </button>
      </footer>
    </div>
  );
}
