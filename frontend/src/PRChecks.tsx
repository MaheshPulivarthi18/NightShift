import { FormEvent, useState } from "react";
import { api, PRCheckRow, PRCheckView } from "./api";
import { Html, Log, StatusBadge, Title, ago, money, usePoll } from "./ui";

// Check any pull request as a careful maintainer would: guidelines, evidence, run it, review it.
export function PRChecks() {
  const [rows, error, reload] = usePoll(api.prChecks, 4000, []);
  return (
    <div className="page">
      <section className="page-head">
        <div>
          <h1>PR checks</h1>
          <p className="muted">
            Checks a pull request's guidelines and evidence, runs it in the sandbox (its tests must fail without its change and
            pass with it; the linked issue's case too), and reviews it. Nothing is posted.
          </p>
        </div>
        <NewCheck onStarted={reload} />
      </section>
      {error && <p className="error">{error}</p>}
      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>Pull request</th>
              <th>Verdict</th>
              <th className="right">Must / should</th>
              <th className="right">Cost</th>
              <th>Checked</th>
            </tr>
          </thead>
          <tbody>
            {rows?.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">No PR checked yet. Paste a pull request URL above.</td>
              </tr>
            )}
            {rows?.map((c) => (
              <tr key={c.id} className="clickable" onClick={() => (window.location.hash = `#/checks/${c.id}`)}>
                <td>
                  <span className="mono dim">{c.repo}#{c.number}</span> <Title text={c.title} />
                </td>
                <td><Verdict c={c} /></td>
                <td className="right mono">{c.status === "done" ? `${c.must} / ${c.should}` : ""}</td>
                <td className="right mono">{money(c.cost_usd)}</td>
                <td className="dim">{ago(c.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Verdict({ c }: { c: PRCheckRow }) {
  if (c.status !== "done") return <StatusBadge status={c.status === "running" ? "running" : "failed"} />;
  const kind = c.must ? "bad" : c.should ? "warn" : "ok";
  return <span className={`check-verdict check-${kind}`}>{c.verdict}</span>;
}

function NewCheck({ onStarted }: { onStarted: () => void }) {
  const [url, setUrl] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    api.checkPr(url.trim()).then(
      () => (setUrl(""), setMsg("Started: it shows below in a moment."), window.setTimeout(onStarted, 1500)),
      (err: Error) => setMsg(err.message),
    ).finally(() => setBusy(false));
  };
  return (
    <form className="inline-form" onSubmit={submit}>
      <input className="mono" placeholder="https://github.com/owner/repo/pull/123" value={url}
        onChange={(e) => setUrl(e.target.value)} required size={44} />
      <button className="primary" disabled={busy}>{busy ? "Starting…" : "Check PR"}</button>
      {msg && <span className="muted">{msg}</span>}
    </form>
  );
}

export function PRCheckPage({ id, embedded = false }: { id: number; embedded?: boolean }) {
  const [c, error, reload] = usePoll<PRCheckView>(() => api.prCheck(id), 5000, [id]);
  if (error) return <div className="page"><a className="back" href="#/checks">← PR checks</a><p className="error">{error}</p></div>;
  if (!c) return <p className="muted page">Loading…</p>;
  return (
    <div className="page narrow">
      {!embedded && <a className="back" href="#/checks">← PR checks</a>}
      <header className="page-head">
        <div>
          <h1><Title text={c.title} /></h1>
          <p className="muted">
            <a href={c.url} target="_blank" rel="noreferrer">{c.repo}#{c.number} ↗</a> by @{c.author} · {money(c.cost_usd)} ·{" "}
            {ago(c.created_at)}
          </p>
        </div>
      </header>
      {c.status === "running" && <Log runId={c.run_id} onStatus={reload} />}
      {c.status === "failed" && <p className="error">The check stopped: {c.error}</p>}
      {c.status === "done" && (
        <article className="report">
          <Html html={c.html} />
        </article>
      )}
    </div>
  );
}
