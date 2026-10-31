import { Fragment, useState } from "react";
import { api, CompareRow, LiveCheck } from "./api";
import { ago, usePoll } from "./ui";

// Where to contribute first: every watched repo, from its latest contributor report, ranked by how outside
// PRs fare there and how much there is to start on. Whether a starter issue is still free is checked live.

const VERDICT_RANK: Record<string, number> = {
  welcoming: 0, active: 1, slow: 2, cautious: 3, mixed: 3, hard: 4, unwelcoming: 5, inactive: 6, closed: 7,
};
type Sort = "best" | "merge" | "response" | "starters";
const SORTS: { key: Sort; label: string }[] = [
  { key: "best", label: "Best first" },
  { key: "merge", label: "Merge rate" },
  { key: "response", label: "Response time" },
  { key: "starters", label: "Starter issues" },
];

const ready = (r: CompareRow) => r.starters.filter((s) => s.size !== "large").length;

function rank(rows: CompareRow[], sort: Sort): CompareRow[] {
  const verdict = (r: CompareRow) => (r.pulse ? VERDICT_RANK[r.pulse.verdict] ?? 8 : 9);
  const response = (r: CompareRow) => r.pulse?.median_pr_response_days ?? 999;
  const merge = (r: CompareRow) => r.pulse?.merge_rate ?? -1;
  const by: Record<Sort, (a: CompareRow, b: CompareRow) => number> = {
    // Welcoming first; among equals, more small and medium starter issues, then faster answers.
    best: (a, b) => verdict(a) - verdict(b) || ready(b) - ready(a) || response(a) - response(b),
    merge: (a, b) => merge(b) - merge(a) || verdict(a) - verdict(b),
    response: (a, b) => response(a) - response(b) || verdict(a) - verdict(b),
    starters: (a, b) => ready(b) - ready(a) || verdict(a) - verdict(b),
  };
  return [...rows].sort(by[sort]);
}

/** `initial`: a repo whose row starts open (#/compare?open=owner/name), e.g. linked from elsewhere. */
export function Compare({ initial = null }: { initial?: string | null }) {
  const [rows, error, reload] = usePoll<CompareRow[]>(api.compare, 5000, []);
  const [sort, setSort] = useState<Sort>("best");
  const [open, setOpen] = useState<string | null>(initial);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  if (error) return <p className="error">{error}</p>;
  if (!rows) return <p className="muted">Loading…</p>;
  const refreshing = rows.filter((r) => r.refresh === "queued" || r.refresh === "running");
  const refresh = () => {
    setBusy(true);
    api.refreshCompare().then(
      (r) => {
        setNote(r.queued.length ? `Refreshing ${r.queued.length} reports, two at a time.` : "Every report is less than a week old.");
        setBusy(false);
        reload();
      },
      (e: Error) => (setNote(e.message), setBusy(false)),
    );
  };
  const done = rows.filter((r) => r.refresh === "done" || r.refresh === "failed").length;
  return (
    <div className="compare">
      <header className="page-head">
        <div>
          <h1>Where to contribute</h1>
          <p className="muted">
            Your {rows.length} watched repos, from each one's contributor report: how outside contributors' PRs fared in the
            last 90 days, how fast maintainers answer, and the issues you could start on.
          </p>
        </div>
        <div className="compare-actions">
          {refreshing.length > 0 ? (
            <span className="lamp-text">Refreshing reports: {done} of {done + refreshing.length} done</span>
          ) : (
            <button onClick={refresh} disabled={busy} title="Runs the report of every repo with none, or one older than a week">
              {busy ? "Starting…" : "Refresh stale reports"}
            </button>
          )}
        </div>
      </header>
      {note && <p className="muted">{note}</p>}
      <div className="seg compare-sort" role="group" aria-label="Sort by">
        {SORTS.map((s) => (
          <button key={s.key} className={sort === s.key ? "on" : ""} onClick={() => setSort(s.key)} aria-pressed={sort === s.key}>
            {s.label}
          </button>
        ))}
      </div>
      <div className="table-wrap">
        <table className="grid compare-grid">
          <thead>
            <tr>
              <th>#</th>
              <th>Repo</th>
              <th>Outside PRs, 90 days</th>
              <th>First answer</th>
              <th title="Outside PRs waiting a week or more with no maintainer response">Unanswered</th>
              <th title="Free issues an outsider can take without asking first: small · medium · large">Starter issues</th>
              <th>AI rules</th>
              <th>Report</th>
            </tr>
          </thead>
          <tbody>
            {rank(rows, sort).map((r, i) => (
              <Fragment key={r.repo}>
                <Row r={r} n={i + 1} open={open === r.repo} onToggle={() => setOpen(open === r.repo ? null : r.repo)} />
                {open === r.repo && <Detail r={r} />}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <p className="dim small-print">
        "Best first" puts welcoming repos first, then the ones with more small and medium starter issues, then faster
        answers. Outside contributors are authors who are not the repo's owner, members or collaborators. A report can be
        days old: check its starter issues are still free before you plan one.
      </p>
    </div>
  );
}

function Row({ r, n, open, onToggle }: { r: CompareRow; n: number; open: boolean; onToggle: () => void }) {
  const p = r.pulse;
  const sizes = (["small", "medium", "large"] as const).map((s) => r.starters.filter((x) => x.size === s).length);
  const state = r.refresh === "running" ? "refreshing…" : r.refresh === "queued" ? "queued" : r.refresh === "failed" ? "refresh failed" : null;
  return (
    <tr className={`compare-row${open ? " open" : ""}`} onClick={onToggle} aria-expanded={open}>
      <td className="mono dim">{p ? n : ""}</td>
      <td>
        <span className="compare-repo">{r.repo}</span>
        {p ? <span className={`verdict verdict-${p.verdict}`}>{p.verdict}</span> : <span className="dim"> no report yet</span>}
      </td>
      <td className="mono">
        {p ? (
          <>
            {p.outside_prs_merged} merged · {p.outside_prs_closed_unmerged} closed
            <span className="dim"> · {p.merge_rate == null ? "–" : `${Math.round(p.merge_rate * 100)}%`}</span>
          </>
        ) : "–"}
      </td>
      <td className="mono">{p?.median_pr_response_days == null ? "–" : `${p.median_pr_response_days} d`}</td>
      <td className={`mono${p && p.outside_prs_waiting >= 10 ? " warn-text" : ""}`}>{p ? p.outside_prs_waiting : "–"}</td>
      <td className="mono">{p ? `${sizes[0]} · ${sizes[1]} · ${sizes[2]}` : "–"}</td>
      <td>{r.ai_rules.length ? <span className="ai-chip" title={r.ai_rules.join("\n")}>{r.ai_rules.length} rule{r.ai_rules.length > 1 ? "s" : ""}</span> : <span className="dim">none</span>}</td>
      <td className="dim">{state ?? (r.report_at ? ago(r.report_at) : "never")}</td>
    </tr>
  );
}

function Detail({ r }: { r: CompareRow }) {
  const [live, setLive] = useState<LiveCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const taken = new Map((live?.taken ?? []).map((t) => [t.number, t.why]));
  const check = () => {
    if (!r.run_id) return;
    setChecking(true);
    api.reportLive(r.repo, r.run_id).then(
      (l) => (setLive(l), setChecking(false)),
      (e: Error) => (setError(e.message), setChecking(false)),
    );
  };
  const free = live ? r.starters.filter((s) => !taken.has(s.number)).length : null;
  return (
    <tr className="compare-detail">
      <td />
      <td colSpan={7}>
        {r.pulse && <p className="muted">{r.pulse.verdict_reason}</p>}
        {r.ai_rules.length > 0 && (
          <div className="ai-rules">
            <b>The repo's rules on AI</b>
            <ul>{r.ai_rules.map((x) => <li key={x}>{x}</li>)}</ul>
          </div>
        )}
        <div className="detail-h">
          <b>Starter issues</b>
          {r.starters.length > 0 && (
            <button className="small" onClick={check} disabled={checking}>
              {checking ? "Checking on GitHub…" : live ? "Check again" : "Check they are still free"}
            </button>
          )}
          {live && <span className="dim">{free} of {r.starters.length} still free · checked {ago(live.at)}</span>}
          {error && <span className="error">{error}</span>}
          <a className="small-link" href={`#/r/${r.repo}/items`}>Open the repo →</a>
        </div>
        {r.starters.length === 0 ? (
          <p className="dim">{r.pulse ? "None in the latest report." : "No report yet: refresh to get one."}</p>
        ) : (
          <ul className="starters">
            {r.starters.map((s) => (
              <li key={s.number} className={taken.has(s.number) ? "taken" : ""}>
                <a href={`https://github.com/${r.repo}/issues/${s.number}`} target="_blank" rel="noreferrer">#{s.number}</a>{" "}
                <span className="starter-title">{s.title || s.summary}</span>
                <span className={`size size-${s.size}`}>{s.size}</span>
                <span className="dim">{s.kind}</span>
                {taken.has(s.number) ? <p className="taken-why">Taken: {taken.get(s.number)}</p> : <p className="dim">{s.first_step}</p>}
              </li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  );
}
