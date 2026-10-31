import { useState } from "react";
import { api, Overview, PRCheckRow, RepoRow, SyncStatus, TaskSummary } from "./api";
import { ago, money, Title, usePoll } from "./ui";

// The global overview: what waits on you across repos, and every repo at a glance.

/** Tasks that wait on the operator: a plan to approve, a fix to publish, a stopped run, review feedback
 * on a published PR that nobody has answered yet, or a published PR whose CI fails. */
export function needsYou(tasks: TaskSummary[]): TaskSummary[] {
  return tasks.filter((t) => {
    if (t.archived || t.busy) return false;
    if (t.asking && t.asked) return t.feedback > 0; // waiting on the maintainers until someone replies
    return ["gate1", "gate2", "stopped"].includes(t.stage) || t.feedback > 0 || (t.ci_failing ?? []).length > 0;
  });
}

function ask(t: TaskSummary): [string, string] {
  if (t.stage === "done" && t.feedback) return [`Reply · ${t.feedback}`, "badge yours"];
  if (t.ci_failing?.length) return ["CI failing", "badge must"];
  if (t.asking) return t.asked ? [`Answered · ${t.feedback}`, "badge yours"] : ["Ask the maintainers", "badge info"];
  return ({ gate1: ["Approve plan", "badge info"], gate2: ["Publish", "badge info"], stopped: ["Stopped", "badge must"] } as
    Record<string, [string, string]>)[t.stage] ?? [t.stage, "badge"];
}

/** When GitHub was last checked for feedback on your PRs, and a button to check now. */
export function SyncBar() {
  const [st, , reload] = usePoll<SyncStatus>(api.syncStatus, 10000, []);
  const [asked, setAsked] = useState(false);
  const running = asked || st?.running;
  const now = () => { setAsked(true); api.syncNow().finally(() => window.setTimeout(() => (setAsked(false), reload()), 4000)); };
  return (
    <span className="sync">
      <span className="status">
        <span className={`dot ${st?.error ? "bad" : st?.at ? "ok" : ""}`} />
        {running ? "Checking GitHub…" : st?.error ? "Last check failed" : st?.at ? `Checked GitHub ${ago(st.at)}` : "Not checked yet"}
      </span>
      <button className="ghost small" onClick={now} disabled={!!running}>Sync now</button>
    </span>
  );
}

export function Home() {
  const [repos] = usePoll<RepoRow[]>(api.repos, 30000, []);
  const [tasks] = usePoll<TaskSummary[]>(() => api.tasks(), 15000, []);
  const [ov] = usePoll<Overview>(api.overview, 30000, []);
  // incoming PRs checked on the sync, on repos you maintain, until you post the review or dismiss it
  const [incoming, , reloadIncoming] = usePoll<PRCheckRow[]>(api.incomingChecks, 30000, []);
  const needs = needsYou(tasks ?? []);
  const waiting = needs.length + (incoming?.length ?? 0);
  const working = (tasks ?? []).filter((t) => t.busy || t.stage === "work" || t.stage === "plan").length;
  const per = (repo: string) =>
    needs.filter((t) => t.repo === repo).length + (incoming ?? []).filter((c) => c.repo === repo).length;
  const dismiss = (id: number) => api.dismissCheck(id).then(reloadIncoming);
  return (
    <div className="page home">
      <header className="page-head"><h1>Overview</h1><SyncBar /></header>
      <div className="stats">
        <div className="stat"><div className="v">{repos?.length ?? "—"}</div><div className="l">Repos</div></div>
        <div className="stat"><div className="v">{waiting}</div><div className="l">Need you</div></div>
        <div className="stat"><div className="v">{working}</div><div className="l">Working now</div></div>
        <div className="stat"><div className="v">{ov ? money(ov.spend.this_month) : "—"}</div><div className="l">Spent this month</div><div className="d">{ov ? `${money(ov.spend.today)} today` : ""}</div></div>
      </div>

      <section className="panel">
        <div className="panel-h"><h2>Needs you</h2><span className="dim">{waiting}</span></div>
        {waiting === 0 && <p className="panel-b dim">Nothing waits on you.</p>}
        {(incoming ?? []).map((c) => (
          <div key={`c${c.id}`} className="row">
            <a className="row-link" href={`#/checks/${c.id}`}>
              <span className={c.must ? "badge must" : "badge info"} title={c.verdict ?? ""}>
                {c.must ? `Review: ${c.must} to fix` : "Review: ready"}
              </span>
              <span className="mono dim">{c.repo.split("/")[1]}#{c.number}</span>
              <span className="grow ell"><Title text={c.title} /></span>
              <span className="dim">@{c.author} · {ago(c.created_at)}</span>
            </a>
            <button className="ghost small" onClick={() => dismiss(c.id)} title="Not to be posted; the check stays under PR checks">Dismiss</button>
          </div>
        ))}
        {needs.map((t) => (
          <a key={t.id} className="row" href={`#/r/${t.repo}/items/t${t.id}`}>
            <span className={ask(t)[1]} title={t.ci_failing?.length ? `Failing: ${t.ci_failing.join(", ")}` : undefined}>{ask(t)[0]}</span>
            <span className="mono dim">{t.repo.split("/")[1]}#{t.issue}</span>
            <span className="grow ell"><Title text={t.title} /></span>
            <span className="dim">{ago(t.updated_at)}</span>
          </a>
        ))}
      </section>

      <section className="panel">
        <div className="panel-h"><h2>Repos</h2></div>
        <div className="table-wrap flat">
          <table className="grid">
            <thead><tr><th>Repo</th><th>View</th><th>Environment</th><th>Worth contributing?</th><th className="right">Need you</th></tr></thead>
            <tbody>
              {repos?.length === 0 && (
                <tr><td colSpan={5} className="dim">No repos yet. Add one with “+ Add repo” in the sidebar: NightShift sets it up for you.</td></tr>
              )}
              {(repos ?? []).map((r) => (
                <tr key={r.full_name} className="clickable" onClick={() => (window.location.hash = `#/r/${r.full_name}/items`)}>
                  <td><b>{r.full_name.split("/")[1]}</b> <span className="dim">{r.full_name.split("/")[0]}</span></td>
                  <td className="dim">{r.role}{r.auto_check ? " · checks new PRs" : ""}</td>
                  <td>{r.environment ? <span className={`badge ${r.environment.status === "ready" ? "ok" : "must"}`}>{r.environment.status}</span> : <span className="dim">not set up</span>}</td>
                  <td>{r.verdict ? <span className={`verdict verdict-${r.verdict}`}>{r.verdict}</span> : <span className="dim">no report yet</span>}</td>
                  <td className="right">{per(r.full_name) || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
