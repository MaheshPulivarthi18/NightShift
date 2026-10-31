import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ChatAction, ChatCard, ChatTurn, ChatView, post } from "./api";
import { Html, Title, money } from "./ui";

/** Talk to Claude about one issue or PR. It can read the repo's code; it drafts but never posts. */
export function Chat({ repo, number }: { repo: string; number: number }) {
  const [view, setView] = useState<ChatView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<{ text: string; tools: string[]; cards: ChatCard[] } | null>(null);
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
    setLive({ text: "", tools: [], cards: [] });
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
          if (ev.type === "action") setLive((l) => l && { ...l, cards: [...l.cards, { action: ev.action, note: ev.note, why: ev.why }] });
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
          <h1><Title text={it.title} /></h1>
          <div className="facts">
            <a href={it.url} target="_blank" rel="noreferrer">on GitHub ↗</a>
            {it.kind === "issue" && it.state === "OPEN" && !view.task && <a href={`#/?repo=${repo}&issue=${it.number}`}>plan a fix →</a>}
            <span className="mono dim">chat cost {money(view.cost_usd)}</span>
            {view.task ? (
              <a href={`#/tasks/${view.task.id}`}>
                task {view.task.id}: {view.task.status.replace("_", " ")}
                {view.task.stopped_at ? ` at ${view.task.stopped_at}` : ""} →
              </a>
            ) : <span className="dim">no NightShift task yet</span>}
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
          ) : t.role === "action" ? (
            <ActionCard key={i} card={t} view={view} repo={repo} number={number} onDone={load} />
          ) : (
            <div key={i} className="turn assistant"><Html html={t.html} /></div>
          ),
        )}
        {live && (
          <>
            {live.tools.length > 0 && <div className="turn tools mono">read {live.tools.join(" · ")}</div>}
            <div className="turn assistant streaming">{live.text || <span className="muted">Thinking…</span>}</div>
            {live.cards.map((c, i) => <ActionCard key={`live-${i}`} card={c} view={view} repo={repo} number={number} onDone={load} />)}
          </>
        )}
        <div ref={bottom} />
      </div>
      {error && <p className="error">{error}</p>}
      <form className="composer" onSubmit={send}>
        <textarea
          rows={3}
          value={text}
          placeholder="Ask, or tell it what to do next (it proposes a step you can run)…  Enter to send"
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

const LABEL: Record<ChatAction, string> = {
  plan: "Plan a fix",
  approve_and_run: "Approve the plan and run",
  continue: "Continue from where it stopped",
  start_over: "Start over from the plan",
  plan_again: "Plan again with this answer",
  review_and_publish: "Review and publish at Gate 2",
  archive: "Archive the task",
};
const NOTE: Partial<Record<ChatAction, string>> = {
  plan: "Instruction to the Planner (optional)",
  approve_and_run: "Your word at Gate 1 (optional)",
  continue: "What the Coder should fix",
  start_over: "What to do differently",
  plan_again: "The answer to the Planner's questions",
};

/** A step the chat proposed. It runs only on a click, through the same endpoints as the task page. */
function ActionCard({ card, view, repo, number, onDone }: {
  card: ChatCard; view: ChatView; repo: string; number: number; onDone: () => void;
}) {
  const [note, setNote] = useState(card.note);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const task = view.task;
  const possible = view.allowed.includes(card.action);
  const needsNote = ["continue", "start_over", "plan_again"].includes(card.action);
  const run = () => {
    const id = task?.id ?? 0;
    const calls: Record<ChatAction, () => Promise<unknown>> = {
      plan: () => api.create(repo, number, "", note.trim()),
      approve_and_run: () => api.decide(id, true, note.trim(), [], []).then(() => api.run(id)),
      continue: () => api.revise(id, note.trim(), false, "continue"),
      start_over: () => api.revise(id, note.trim(), false, "restart"),
      plan_again: () => api.replan(id, note.trim()),
      review_and_publish: async () => { window.location.hash = `#/tasks/${id}`; },
      archive: () => api.archive(id, true),
    };
    setBusy(true);
    calls[card.action]().then(
      () => {
        setMsg({ ok: true, text: card.action === "review_and_publish" ? "Opened Gate 2." : "Started. Follow it on the task page." });
        setBusy(false);
        onDone();
      },
      (e: Error) => (setMsg({ ok: false, text: e.message }), setBusy(false)),
    );
  };
  return (
    <div className={`turn action-card${possible ? "" : " stale"}`}>
      <div className="action-h">
        <span className="gate-mark">Proposed step</span>
        <b>{LABEL[card.action]}</b>
      </div>
      {card.why && <p className="muted">{card.why}</p>}
      {NOTE[card.action] && (
        <label className="full">
          <span>{NOTE[card.action]}</span>
          <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} disabled={!possible || busy || msg?.ok} />
        </label>
      )}
      <div className="actions">
        {possible ? (
          <button className="primary" onClick={run} disabled={busy || msg?.ok || (needsNote && !note.trim())}>
            {busy ? "Starting…" : LABEL[card.action]}
          </button>
        ) : (
          <span className="dim">No longer possible: the task is {task ? task.status.replace("_", " ") : "not created yet"}.</span>
        )}
        {task && <a href={`#/tasks/${task.id}`}>open the task</a>}
        {msg && <span className={msg.ok ? "ok-text" : "error"}>{msg.text}</span>}
      </div>
    </div>
  );
}
