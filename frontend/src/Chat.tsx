import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ChatTurn, ChatView, post } from "./api";
import { Html, money } from "./ui";

/** Talk to Claude about one issue or PR. It can read the repo's code; it drafts but never posts. */
export function Chat({ repo, number }: { repo: string; number: number }) {
  const [view, setView] = useState<ChatView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<{ text: string; tools: string[] } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const load = () => api.chat(repo, number).then(setView, (e: Error) => setError(e.message));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, number]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [view, live]);

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const message = text.trim();
    if (!message || busy) return;
    setBusy(true);
    setText("");
    setLive({ text: "", tools: [] });
    setView((v) => v && { ...v, history: [...v.history, { role: "user", text: message } as ChatTurn] });
    try {
      const res = await post(`/chat/${repo}/${number}`, { message });
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines.filter(Boolean)) {
          const ev = JSON.parse(line);
          if (ev.type === "text") setLive((l) => l && { ...l, text: l.text + ev.text });
          if (ev.type === "tool") setLive((l) => l && { ...l, tools: [...l.tools, `${ev.name} ${Object.values(ev.input ?? {})[0] ?? ""}`] });
          if (ev.type === "error") setError(ev.message);
        }
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLive(null);
      setBusy(false);
      load();
    }
  };

  if (!view) return <div className="page"><a className="back" href="#/repos">← Repos</a>{error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}</div>;
  const it = view.item;
  return (
    <div className="page narrow chat">
      <a className="back" href={`#/repos/${repo}/report`}>← {repo} report</a>
      <header className="page-head">
        <div>
          <div className="mono dim">
            {repo}#{it.number} · {it.kind === "pr" ? "pull request" : "issue"} · {it.state.toLowerCase()}
            {it.author && ` · by ${it.author}`}
          </div>
          <h1>{it.title}</h1>
          <div className="facts">
            <a href={it.url} target="_blank" rel="noreferrer">on GitHub ↗</a>
            {it.kind === "issue" && it.state === "OPEN" && <a href={`#/?repo=${repo}&issue=${it.number}`}>plan a fix →</a>}
            <span className="mono dim">chat cost {money(view.cost_usd)}</span>
          </div>
        </div>
      </header>
      <details className="item-body">
        <summary>Description</summary>
        <Html html={view.body_html} />
      </details>

      <div className="turns">
        {view.history.length === 0 && !live && (
          <p className="muted">Ask about this {it.kind === "pr" ? "pull request" : "issue"}: what it needs, where the code is, how to fix it. Claude can read the repo; it never posts.</p>
        )}
        {view.history.map((t, i) =>
          t.role === "user" ? (
            <div key={i} className="turn user">{t.text}</div>
          ) : t.role === "tools" ? (
            <div key={i} className="turn tools mono">read {t.tools.join(" · ")}</div>
          ) : (
            <div key={i} className="turn assistant"><Html html={t.html} /></div>
          ),
        )}
        {live && (
          <>
            {live.tools.length > 0 && <div className="turn tools mono">read {live.tools.join(" · ")}</div>}
            <div className="turn assistant streaming">{live.text || <span className="muted">Thinking…</span>}</div>
          </>
        )}
        <div ref={bottom} />
      </div>
      {error && <p className="error">{error}</p>}
      <form className="composer" onSubmit={send}>
        <textarea
          rows={3}
          value={text}
          placeholder="Ask a question…  (Enter to send, Shift+Enter for a new line)"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) send(e);
          }}
          disabled={busy}
        />
        <button className="primary" disabled={busy || !text.trim()}>{busy ? "Answering…" : "Send"}</button>
      </form>
    </div>
  );
}
