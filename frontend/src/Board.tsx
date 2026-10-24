import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, Stage, TaskSummary } from "./api";
import { StatusBadge, money } from "./ui";

const LANES: { key: string; title: string; stages: Stage[]; hint: string }[] = [
  { key: "you", title: "Waiting on you", stages: ["gate1", "gate2"], hint: "Approve a plan or a PR" },
  { key: "work", title: "In progress", stages: ["plan", "work"], hint: "Planning, coding, reviewing" },
  { key: "done", title: "Published", stages: ["done"], hint: "PRs opened" },
  { key: "stopped", title: "Stopped", stages: ["stopped", "closed"], hint: "Needs you, failed or rejected" },
];

export function Board() {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState(0);

  const load = useCallback(() => {
    api.tasks().then(setTasks, (e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  const lanes = useMemo(
    () => LANES.map((lane) => ({ ...lane, tasks: (tasks ?? []).filter((t) => lane.stages.includes(t.stage)) })),
    [tasks],
  );
  const ordered = useMemo(() => lanes.flatMap((l) => l.tasks), [lanes]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "j") setCursor((c) => Math.min(c + 1, ordered.length - 1));
      if (e.key === "k") setCursor((c) => Math.max(c - 1, 0));
      if (e.key === "Enter" && ordered[cursor]) window.location.hash = `#/tasks/${ordered[cursor].id}`;
      if (e.key === "n") {
        e.preventDefault();
        document.getElementById("new-repo")?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ordered, cursor]);

  const waiting = lanes[0].tasks.length;
  const spent = (tasks ?? []).reduce((sum, t) => sum + t.cost_usd, 0);

  return (
    <div className="board">
      <section className="board-head">
        <div>
          <h1>Tasks</h1>
          <p className="muted">
            {tasks === null ? "Loading…" : `${tasks.length} tasks · ${waiting} waiting on you · ${money(spent)} spent`}
          </p>
        </div>
        <NewTask onCreated={load} />
      </section>
      {error && <p className="error">{error}</p>}
      <div className="lanes">
        {lanes.map((lane, li) => (
          <section key={lane.key} className={`lane lane-${lane.key}`} style={{ animationDelay: `${li * 60}ms` }}>
            <header>
              <h2>{lane.title}</h2>
              <span className="count">{lane.tasks.length}</span>
            </header>
            <p className="lane-hint">{lane.hint}</p>
            {lane.tasks.length === 0 && <p className="empty">Nothing here.</p>}
            {lane.tasks.map((t) => (
              <a
                key={t.id}
                href={`#/tasks/${t.id}`}
                className={`card${ordered[cursor]?.id === t.id ? " cursor" : ""}`}
                onMouseEnter={() => setCursor(ordered.findIndex((o) => o.id === t.id))}
              >
                <div className="card-top">
                  <span className="mono dim">
                    {t.repo}#{t.issue}
                  </span>
                  <StatusBadge status={t.status} busy={t.busy} />
                </div>
                <div className="card-title">{t.title}</div>
                <div className="card-foot mono dim">
                  <span>task {t.id}</span>
                  {t.rehearsal && <span className="tag">rehearsal</span>}
                  <span className="spacer" />
                  <span>{money(t.cost_usd)}</span>
                </div>
                {t.error && t.stage === "stopped" && <div className="card-error">{t.error}</div>}
              </a>
            ))}
          </section>
        ))}
      </div>
    </div>
  );
}

function NewTask({ onCreated }: { onCreated: () => void }) {
  const [repo, setRepo] = useState("");
  const [issue, setIssue] = useState("");
  const [base, setBase] = useState("");
  const [rehearsal, setRehearsal] = useState(false);
  const [state, setState] = useState<{ busy: boolean; msg: string | null }>({ busy: false, msg: null });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setState({ busy: true, msg: null });
    api.create(repo.trim(), Number(issue), base.trim(), rehearsal).then(
      () => {
        setState({ busy: false, msg: "Planning started. The plan appears under “Waiting on you” in a minute or two." });
        setIssue("");
        onCreated();
      },
      (err: Error) => setState({ busy: false, msg: err.message }),
    );
  };

  return (
    <form className="new-task" onSubmit={submit}>
      <label>
        <span>Repo</span>
        <input id="new-repo" className="mono" placeholder="owner/name" value={repo} onChange={(e) => setRepo(e.target.value)} required pattern="[\w.-]+/[\w.-]+" />
      </label>
      <label className="narrow">
        <span>Issue</span>
        <input className="mono" inputMode="numeric" placeholder="42" value={issue} onChange={(e) => setIssue(e.target.value)} required pattern="\d+" />
      </label>
      <label className="narrow">
        <span>Base</span>
        <input className="mono" placeholder="default" value={base} onChange={(e) => setBase(e.target.value)} />
      </label>
      <label className="check">
        <input type="checkbox" checked={rehearsal} onChange={(e) => setRehearsal(e.target.checked)} />
        <span>Rehearsal</span>
      </label>
      <button className="primary" disabled={state.busy}>
        {state.busy ? "Starting…" : "Plan issue"} <kbd>n</kbd>
      </button>
      {state.msg && <p className="form-msg">{state.msg}</p>}
    </form>
  );
}
