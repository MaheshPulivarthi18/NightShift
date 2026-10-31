import { FormEvent, useState } from "react";
import { api, RecordPR, TrackRecord as Record_ } from "./api";
import { money, usePoll } from "./ui";

// The track record: every pull request made with NightShift, and how it went with the maintainers.

const ORDER: RecordPR["outcome"][] = ["merged", "approved", "changes requested", "waiting", "closed", "unknown"];

function days(n: number | null): string {
  return n == null ? "" : n < 1 ? `${Math.round(n * 24)} h` : `${n.toFixed(n < 10 ? 1 : 0)} d`;
}

export function TrackRecord() {
  const [data, error, reload] = usePoll<Record_>(api.trackRecord, 60000, []);
  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const s = data.summary;
  return (
    <div className="record">
      <header className="page-head">
        <div>
          <h1>Track record</h1>
          <p className="muted">
            What happened to the pull requests made with NightShift, as GitHub shows them now, and what they cost.
          </p>
        </div>
      </header>
      {s.prs === 0 ? (
        <p className="muted">No pull requests yet. Publish one from a task, or add one you made earlier below.</p>
      ) : (
        <>
          <div className="tiles">
            <Tile n={s.prs} label="pull requests" />
            <Tile n={s.accepted} label="merged or approved" tone="ok" sub={`${s.merged} merged · ${s.approved} approved`} />
            <Tile n={s.waiting + s.changes_requested} label="open" sub={`${s.waiting} waiting · ${s.changes_requested} changes asked`} />
            <Tile n={s.closed} label="closed unmerged" tone={s.closed ? "bad" : undefined} />
            <Tile n={days(s.median_days_to_merge) || "–"} label="median time to merge" />
            <Tile n={s.median_cost_usd == null ? "–" : money(s.median_cost_usd)} label="median cost per PR"
                  sub={`${money(s.total_cost_usd)} over ${s.with_cost} with costs`} />
          </div>
          <div className="table-wrap">
            <table className="grid record-grid">
              <thead>
                <tr><th>Pull request</th><th>Outcome</th><th>Opened</th><th>Merged in / open for</th><th>Cost</th><th>Attempts</th><th>Understood</th></tr>
              </thead>
              <tbody>
                {[...data.prs].sort((a, b) => ORDER.indexOf(a.outcome) - ORDER.indexOf(b.outcome) || (b.opened ?? "").localeCompare(a.opened ?? "")).map((p) => (
                  <tr key={p.url}>
                    <td>
                      <a href={p.url} target="_blank" rel="noreferrer">{p.repo}#{p.number}</a>{" "}
                      <span className="dim">{p.title.slice(0, 70)}</span>
                    </td>
                    <td><span className={`outcome outcome-${p.outcome.replace(" ", "-")}`}>{p.outcome}</span></td>
                    <td className="mono dim">{p.opened?.slice(0, 10) ?? ""}</td>
                    <td className="mono">{p.days_to_merge != null ? days(p.days_to_merge) : p.days_open != null ? <span className="dim">{days(p.days_open)}</span> : ""}</td>
                    <td className="mono">{p.imported ? <span className="dim">not recorded</span> : money(p.cost_usd)}</td>
                    <td className="mono">{p.imported ? "" : p.attempts}</td>
                    <td>{p.understood == null ? <span className="dim">–</span> : p.understood ? "passed" : <span className="warn-text">not passed</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="dim small-print">
            Outcomes come from GitHub: approved and changes asked are the reviewers' latest reviews on an open pull request.
            Merge rate {s.merge_rate == null ? "–" : `${Math.round(s.merge_rate * 100)}%`} of those decided; cost per merged
            pull request {s.cost_per_merged_usd == null ? "–" : money(s.cost_per_merged_usd)}.
          </p>
        </>
      )}
      <AddEarlier onAdded={reload} />
    </div>
  );
}

function Tile({ n, label, sub, tone }: { n: number | string; label: string; sub?: string; tone?: "ok" | "bad" }) {
  return (
    <div className={`tile${tone ? ` tile-${tone}` : ""}`}>
      <div className="tile-n">{n}</div>
      <div className="tile-label">{label}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  );
}

function AddEarlier({ onAdded }: { onAdded: () => void }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    api.addToRecord(url.trim()).then(
      () => (setUrl(""), setMsg(null), setBusy(false), onAdded()),
      (err: Error) => (setMsg(err.message), setBusy(false)),
    );
  };
  return (
    <form className="add-earlier" onSubmit={submit}>
      <b>Add a pull request you made earlier</b>
      <span className="dim">One of yours from before NightShift kept its task, or opened by hand. It counts here only.</span>
      <div className="actions">
        <input className="mono" placeholder="https://github.com/owner/repo/pull/123" value={url} onChange={(e) => setUrl(e.target.value)} aria-label="Pull request link" />
        <button className="primary" disabled={busy || !url.trim()}>{busy ? "Adding…" : "Add"}</button>
        {msg && <span className="error">{msg}</span>}
      </div>
    </form>
  );
}
