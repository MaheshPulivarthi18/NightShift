import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, Stage, TaskSummary } from "./api";
import { StatusBadge, Title, money } from "./ui";

const LANES: { key: string; title: string; stages: Stage[]; hint: string }[] = [
  { key: "you", title: "Waiting on you", stages: ["gate1", "gate2"], hint: "Approve a plan or a PR" },
  { key: "work", title: "In progress", stages: ["plan", "work"], hint: "Planning, coding, reviewing" },
  { key: "done", title: "Published", stages: ["done"], hint: "PRs opened" },
  { key: "stopped", title: "Stopped", stages: ["stopped", "closed"], hint: "Needs you, failed or rejected" },
];

export function Board({ prefillRepo, prefillIssue }: { prefillRepo: string; prefillIssue: string }) {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState(0);
  const [archived, setArchived] = useState(false);

  const load = useCallback(() => {
    api.tasks(archived).then(setTasks, (e: Error) => setError(e.message));
  }, [archived]);

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
          <h1>{archived ? "Archived tasks" : "Tasks"}</h1>
          <p className="muted">
            {tasks === null ? "Loading…" : `${tasks.length} tasks · ${waiting} waiting on you · ${money(spent)} spent`}{" "}
            <button className="ghost small" onClick={() => (setTasks(null), setArchived(!archived))}>
              {archived ? "Back to the board" : "Show archived"}
            </button>
          </p>
        </div>
        <NewTask key={prefillRepo + prefillIssue} repo0={prefillRepo} issue0={prefillIssue} onCreated={load} />
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
                <div className="card-title"><Title text={t.title} /></div>
                <div className="card-foot mono dim">
                  <span>task {t.id}</span>
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

// "#705 is not free: open PR #706 by someone references it": each #N links to GitHub, so you can see who took it.
function IssueLinks({ text, repo }: { text: string; repo: string }) {
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) return <>{text}</>;
  return (
    <>
      {text.split(/(#\d+)/).map((part, i) =>
        /^#\d+$/.test(part) ? (
          <a key={i} href={`https://github.com/${repo}/issues/${part.slice(1)}`} target="_blank" rel="noreferrer">
            {part}
          </a>
        ) : (
          part
        ),
      )}
    </>
  );
}

// A PR of yours that NightShift did not open, so its review feedback can be addressed here.
function FromPr() {
  const [url, setUrl] = useState("");
  const [state, setState] = useState<{ busy: boolean; msg: string | null }>({ busy: false, msg: null });
  const start = () => {
    setState({ busy: true, msg: null });
    api.fromPr(url.trim()).then(
      (t) => (window.location.hash = `#/tasks/${t.id}`),
      (e: Error) => setState({ busy: false, msg: e.message }),
    );
  };
  return (
    <div className="from-pr">
      <span>Or follow up a pull request of yours (to address its review feedback here)</span>
      <div className="row">
        <input className="mono" placeholder="https://github.com/owner/repo/pull/123" value={url} onChange={(e) => setUrl(e.target.value)} />
        <button type="button" className="ghost small" disabled={!url.trim() || state.busy} onClick={start}>
          {state.busy ? "Importing…" : "Follow up"}
        </button>
      </div>
      {state.msg && <span className="error">{state.msg}</span>}
    </div>
  );
}

function NewTask({ repo0, issue0, onCreated }: { repo0: string; issue0: string; onCreated: () => void }) {
  const [repo, setRepo] = useState(repo0);
  const [issue, setIssue] = useState(issue0);
  const [base, setBase] = useState("");
  const [note, setNote] = useState("");
  const [state, setState] = useState<{ busy: boolean; msg: string | null; error?: boolean }>({ busy: false, msg: null });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setState({ busy: true, msg: null });
    api.create(repo.trim(), Number(issue), base.trim(), note.trim()).then(
      (r) => {
        const moved = r.note ? ` ${r.note}.` : "";
        setState({ busy: false, msg: `Planning started.${moved} The plan appears under “Waiting on you” in about a minute.` });
        window.location.hash = "#/";
        setIssue("");
        setNote("");
        onCreated();
      },
      (err: Error) => setState({ busy: false, msg: err.message, error: true }),
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
      <details className="options">
        <summary>Options</summary>
        <label>
          <span>Base commit</span>
          <input className="mono" placeholder="default branch" value={base} onChange={(e) => setBase(e.target.value)} />
        </label>
        <FromPr />
        <label className="note">
          <span>Note to the Planner (your instruction; it sets the scope)</span>
          <textarea
            rows={2}
            placeholder="e.g. Treat this as a docs task: add a section on header files with clangd to the README."
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </label>
      </details>
      <button className="primary" disabled={state.busy}>
        {state.busy ? "Starting…" : "Plan issue"}
      </button>
      {state.msg && (
        <p className={`form-msg${state.error ? " error" : ""}`}>
          {state.error ? <IssueLinks text={state.msg} repo={repo.trim()} /> : state.msg}
        </p>
      )}
    </form>
  );
}
