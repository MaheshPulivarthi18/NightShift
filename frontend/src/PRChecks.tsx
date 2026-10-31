import { FormEvent, useState } from "react";
import { api, Finding, PRCheckRow, PRCheckView, ReviewDraft, ReviewEvent } from "./api";
import { Html, Log, StatusBadge, Title, ago, money, usePoll } from "./ui";

// Check any pull request as a careful maintainer would: guidelines, evidence, run it, review it.
export function PRChecks() {
  const [rows, error, reload] = usePoll(api.prChecks, 4000, []);
  const [acc] = usePoll(api.findingAccuracy, 30000, []);
  return (
    <div className="page">
      <section className="page-head">
        <div>
          <h1>PR checks</h1>
          <p className="muted">
            Checks a pull request's guidelines and evidence, runs it in the sandbox (its tests must fail without its change and
            pass with it; the linked issue's case too), and reviews it. Nothing is posted unless you post the review yourself.
          </p>
          {acc && acc.marked > 0 && (
            <p className="dim">Findings you marked right: {acc.right} of {acc.marked} ({Math.round((acc.share ?? 0) * 100)}%).</p>
          )}
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
      {c.status === "done" && c.findings.length > 0 && <Marks c={c} onChange={reload} />}
      {c.status === "done" && <PostReview c={c} onPosted={reload} />}
    </div>
  );
}

/** Was each finding right? Your marks measure the checker (PR checks page) and stay with the check. */
function Marks({ c, onChange }: { c: PRCheckView; onChange: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const mark = (f: Finding, m: "right" | "wrong") =>
    api.markFinding(c.id, f.id, f.mark === m ? null : m).then(onChange, (e: Error) => setError(e.message));
  return (
    <section className="panel">
      <div className="panel-h"><h2>Was it right?</h2><span className="dim">{c.findings.filter((f) => f.mark).length} of {c.findings.length} marked</span></div>
      <div className="panel-b marks">
        {c.findings.map((f) => (
          <div key={f.id} className="row-flex mark-row">
            <span className={`badge ${f.kind === "must" ? "must" : ""}`}>{f.kind === "must" ? "must" : "should"}</span>
            <span className="grow">{f.text}</span>
            <button className={`small ${f.mark === "right" ? "primary" : "ghost"}`} onClick={() => mark(f, "right")} aria-pressed={f.mark === "right"}>Right</button>
            <button className={`small ${f.mark === "wrong" ? "danger" : "ghost"}`} onClick={() => mark(f, "wrong")} aria-pressed={f.mark === "wrong"}>Wrong</button>
          </div>
        ))}
        {error && <p className="error">{error}</p>}
      </div>
    </section>
  );
}

const EVENT_LABELS: Record<ReviewEvent, string> = {
  COMMENT: "Comment",
  REQUEST_CHANGES: "Request changes",
  APPROVE: "Approve",
};

// The report as a review on the PR, under your name: drafted from the report, edited here, posted on confirm.
function PostReview({ c, onPosted }: { c: PRCheckView; onPosted: () => void }) {
  const [draft, setDraft] = useState<ReviewDraft | null>(null);
  const [body, setBody] = useState("");
  const [event, setEvent] = useState<ReviewEvent>("COMMENT");
  const [attribution, setAttribution] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const target = `${c.repo}#${c.number}`;

  const open = () => {
    setBusy(true);
    setMsg(null);
    api.reviewPr(c.id, false).then(
      (r) => (setDraft(r), setBody(r.body), setEvent(r.event)),
      (e: Error) => setMsg(e.message),
    ).finally(() => setBusy(false));
  };
  const recheck = (ev: ReviewEvent) => {
    setEvent(ev);
    api.reviewPr(c.id, false, { body, event: ev }).then((r) => setDraft(r), (e: Error) => setMsg(e.message));
  };
  const post = () => {
    setBusy(true);
    setMsg(null);
    api.reviewPr(c.id, true, { body, event, attribution }).then(
      (r) => (setDraft(r), onPosted()),
      (e: Error) => setMsg(e.message),
    ).finally(() => setBusy(false));
  };

  return (
    <section className="gate">
      <header>
        <h2>Post as a review on GitHub</h2>
        <p className="muted">
          Posts this report on <a href={c.url} target="_blank" rel="noreferrer">{target}</a> as a review under your name, on the
          commit that was checked. Read and edit it first: it speaks for you.
        </p>
      </header>
      {c.review_url && !draft?.posted && (
        <p className="ok">Posted: <a href={c.review_url} target="_blank" rel="noreferrer">{c.review_url}</a></p>
      )}
      {!draft && (
        <div className="actions">
          <button className="ghost" onClick={open} disabled={busy}>{busy ? "Drafting…" : "Draft the review"}</button>
          {msg && <span className="error">{msg}</span>}
        </div>
      )}
      {draft?.posted && draft.review_url && (
        <p className="ok">Posted: <a href={draft.review_url} target="_blank" rel="noreferrer">{draft.review_url}</a></p>
      )}
      {draft && !draft.posted && (
        <div className="gate-result">
          {draft.warnings.map((w) => (
            <p key={w} className="warn">{w}</p>
          ))}
          <div className="publish-form">
            <label className="full">
              <span>Review</span>
              <textarea className="mono" rows={18} value={body} onChange={(e) => setBody(e.target.value)} />
            </label>
            <div className="actions">
              {draft.events.map((ev) => (
                <label key={ev} className="check">
                  <input type="radio" name={`event-${c.id}`} checked={event === ev} onChange={() => recheck(ev)} />
                  <span>{EVENT_LABELS[ev]}</span>
                </label>
              ))}
            </div>
            {draft.attribution_line && (
              <label className="check">
                <input type="checkbox" checked={attribution} onChange={(e) => setAttribution(e.target.checked)} />
                <span>End with the line "{draft.attribution_line}"</span>
              </label>
            )}
          </div>
          <div className="confirm">
            <label>
              <span>Type <b className="mono">{target}</b> to post this review under your name</span>
              <input className="mono" value={typed} onChange={(e) => setTyped(e.target.value)} />
            </label>
            <button className="danger" disabled={typed !== target || busy || (!body.trim() && event !== "APPROVE")} onClick={post}>
              {busy ? "Posting…" : `Post review (${EVENT_LABELS[event]})`}
            </button>
          </div>
          {msg && <p className="error">{msg}</p>}
        </div>
      )}
    </section>
  );
}
