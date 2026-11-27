import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { API_URL, sendChat, waitForHealth } from "./api";

// New session on every page load (1-128 chars).
const newSessionId = () => "s-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

const SIMULATIONS = [
  { value: "", label: "Off" },
  { value: "key_1=429", label: "key_1 → 429" },
  { value: "key_1=500", label: "key_1 → 500" },
  { value: "*=429", label: "All keys → 429" },
];

const EXAMPLES = ["What is the capital of France?", "My name is Priyank", "Explain caching in one sentence"];

const ICONS = {
  plus: "M12 5v14M5 12h14",
  up: "M12 19V5M5 12l7-7 7 7",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  sun: "M12 3v2M12 19v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M3 12h2M19 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  check: "M20 6 9 17l-5-5",
  redo: "M21 4v6h-6M20.5 15a9 9 0 1 1-2.1-9.4L21 10",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
  sliders: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
};

function Icon({ name, size = 16 }) {
  return (
    <svg className="ico" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path d={ICONS[name]} />
    </svg>
  );
}

/** Open/close state for a popover that closes on outside click or Escape. */
function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    const onKey = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, ref };
}

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("theme");
    } catch {
      return null;
    }
  });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }, [theme]);
  const toggle = () => {
    const current = theme ?? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    setTheme(next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      // storage unavailable: theme still applies for this visit
    }
  };
  const isDark = theme ? theme === "dark" : window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  return { isDark, toggle };
}

function Customize({ systemPrompt, setSystemPrompt, simulate, setSimulate }) {
  const pop = usePopover();
  const active = Boolean(systemPrompt.trim() || simulate);
  return (
    <div className="customize" ref={pop.ref}>
      <button
        type="button"
        className={`chip-btn ${active ? "active" : ""}`}
        onClick={() => pop.setOpen((o) => !o)}
        aria-expanded={pop.open}
      >
        <Icon name="sliders" size={14} />
        Customize
        {active && <span className="dot" aria-label="custom settings active" />}
      </button>
      {pop.open && (
        <div className="popover settings" role="dialog" aria-label="Customize">
          <label className="field">
            <span>System prompt</span>
            <textarea
              data-testid="system-prompt-input"
              rows={3}
              placeholder="e.g. You are a concise assistant that answers like a pirate."
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
            <small>Applies to every message in this session.</small>
          </label>
          <div className="field">
            <span>Failure simulation</span>
            <div className="seg" role="radiogroup" aria-label="Failure simulation">
              {SIMULATIONS.map((s) => (
                <button
                  key={s.value}
                  type="button"
                  role="radio"
                  aria-checked={simulate === s.value}
                  className={simulate === s.value ? "on" : ""}
                  onClick={() => setSimulate(s.value)}
                >
                  {s.label}
                </button>
              ))}
            </div>
            <small>Demo only. Forces the chosen API keys to fail so you can watch failover.</small>
          </div>
        </div>
      )}
    </div>
  );
}

function Composer({ big, input, setInput, onSend, pending, settings }) {
  const ref = useRef(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [input]);

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      onSend();
    }
  }

  return (
    <div className={`composer ${big ? "big" : ""}`} onClick={() => ref.current?.focus()}>
      <textarea
        ref={ref}
        data-testid="chat-input"
        rows={1}
        placeholder={big ? "Ask anything" : "Reply…"}
        aria-label="Message"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={onKeyDown}
        autoFocus
      />
      <div className="composer-row" onClick={(e) => e.stopPropagation()}>
        <Customize {...settings} />
        <button
          type="button"
          className="send"
          data-testid="send-button"
          onClick={onSend}
          disabled={pending || !input.trim()}
          aria-label={pending ? "Sending" : "Send"}
        >
          {pending ? <span className="spinner" /> : <Icon name="up" />}
        </button>
      </div>
    </div>
  );
}

function MetaInfo({ meta }) {
  const pop = usePopover();
  const listRef = useRef(null);
  useEffect(() => {
    if (pop.open) listRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [pop.open]);
  const cached = meta.resolution_source === "LLM_CACHE";
  return (
    <span className="info" ref={pop.ref}>
      <button
        type="button"
        className="info-btn"
        onClick={() => pop.setOpen((o) => !o)}
        aria-expanded={pop.open}
        title="Request details"
      >
        <span className={`status ${cached ? "cache" : "live"}`} />
        <span data-testid="meta-source">{meta.resolution_source}</span>
        <span className="sep">·</span>
        <span data-testid="meta-latency">{meta.latency_ms} ms</span>
        <Icon name="info" size={13} />
      </button>
      {meta.fallback_attempts > 0 && (
        <span className="warn-tag">
          {meta.fallback_attempts} fallback{meta.fallback_attempts > 1 ? "s" : ""}
        </span>
      )}
      <dl ref={listRef} className={`popover meta ${pop.open ? "open" : ""}`} aria-hidden={!pop.open}>
        <dt>Model</dt>
        <dd data-testid="meta-model">{meta.model}</dd>
        <dt>Input tokens</dt>
        <dd data-testid="meta-input-tokens">{meta.input_tokens}</dd>
        <dt>Output tokens</dt>
        <dd data-testid="meta-output-tokens">{meta.output_tokens}</dd>
        <dt>Key</dt>
        <dd data-testid="meta-key-id">{meta.key_id ?? "—"}</dd>
        <dt>Fallbacks</dt>
        <dd data-testid="meta-fallback-attempts">{meta.fallback_attempts}</dd>
      </dl>
    </span>
  );
}

function CopyButton({ text }) {
  const [done, setDone] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    } catch {
      // clipboard blocked: nothing to do
    }
  }
  return (
    <button type="button" className="icon-btn" onClick={copy} aria-label={done ? "Copied" : "Copy"} title="Copy">
      <Icon name={done ? "check" : "copy"} size={15} />
    </button>
  );
}

function Markdown({ children }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{ a: (props) => <a {...props} target="_blank" rel="noreferrer" /> }}
    >
      {children}
    </ReactMarkdown>
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
  const { isDark, toggle } = useTheme();

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

  const send = useCallback(
    async (override) => {
      const text = typeof override === "string" ? override : input;
      if (!text.trim() || pending) return;
      if (typeof override !== "string") setInput("");
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
    },
    [input, pending, sessionId, systemPrompt, simulate],
  );

  const retry = (i) => {
    for (let j = i - 1; j >= 0; j--) {
      if (messages[j].role === "user") return send(messages[j].content);
    }
  };

  const composerProps = {
    input,
    setInput,
    onSend: send,
    pending,
    settings: { systemPrompt, setSystemPrompt, simulate, setSimulate },
  };
  const empty = messages.length === 0 && !pending;

  return (
    <div className="app">
      <header className="top">
        <button type="button" className="icon-btn" onClick={() => window.location.reload()} title="New chat" aria-label="New chat">
          <Icon name="plus" size={18} />
        </button>
        <span className="title" title={`Session ${sessionId}`}>
          LLM Chatbot
        </span>
        <button type="button" className="icon-btn" onClick={toggle} aria-label="Toggle dark mode" title="Toggle theme">
          <Icon name={isDark ? "sun" : "moon"} size={18} />
        </button>
      </header>

      {server === "waking" && (
        <div className="notice" role="status">
          <span className="spinner" /> Waking up the server. Free hosting sleeps when idle, so this can take up to a minute.
        </div>
      )}
      {server === "down" && (
        <div className="notice error" role="alert">
          Can't reach the server at {API_URL}. You can still send; requests are retried on network errors.
        </div>
      )}

      {empty ? (
        <main className="hero">
          <h1>What can I help with?</h1>
          <Composer big {...composerProps} />
          <div className="suggestions">
            {EXAMPLES.map((ex) => (
              <button type="button" key={ex} onClick={() => setInput(ex)}>
                {ex}
              </button>
            ))}
          </div>
        </main>
      ) : (
        <>
          <main className="thread" aria-live="polite">
            <div className="thread-inner">
              {messages.map((m, i) =>
                m.role === "user" ? (
                  <div key={i} className="msg user">
                    {m.content}
                  </div>
                ) : m.role === "assistant" ? (
                  <div key={i} className="msg assistant" data-testid="assistant-message">
                    <div className="md">
                      <Markdown>{m.content}</Markdown>
                    </div>
                    <div className="msg-foot">
                      <CopyButton text={m.content} />
                      <button type="button" className="icon-btn" onClick={() => retry(i)} disabled={pending} aria-label="Retry" title="Retry">
                        <Icon name="redo" size={15} />
                      </button>
                      <MetaInfo meta={m.metadata} />
                    </div>
                  </div>
                ) : (
                  <div key={i} className="msg error" role="alert">
                    <span data-testid="error-message">{m.content}</span>
                    <button type="button" className="text-btn" onClick={() => retry(i)} disabled={pending}>
                      <Icon name="redo" size={14} /> Try again
                    </button>
                  </div>
                ),
              )}
              {pending && (
                <div className="msg assistant typing" aria-label="Assistant is typing">
                  <span />
                  <span />
                  <span />
                </div>
              )}
              <div ref={endRef} />
            </div>
          </main>
          <footer className="dock">
            <Composer {...composerProps} />
            <p className="hint">Enter to send · Shift + Enter for a new line</p>
          </footer>
        </>
      )}
    </div>
  );
}
