import { useEffect, useState } from "react";
import { api, ScoutRow, ScoutView } from "./api";
import { ago } from "./ui";

// Find repos to add: candidates you do not watch yet, scouted from public GitHub data (no model, no setup), ranked by
// how outside PRs fare there and how well they fit you. Your candidate list stays in this browser.

const KEY = "nightshift.scout.candidates";

function load(): string[] | null {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as string[]) : null;
  } catch {
    return null;
  }
}

function save(list: string[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    /* private window: the list lasts this visit */
  }
}

const REPO = /^[\w.-]+\/[\w.-]+$/;

export function FindRepos({ onAdded }: { onAdded: () => void }) {
  const [list, setList] = useState<string[]>(() => load() ?? []);
  const [draft, setDraft] = useState("");
  const [view, setView] = useState<ScoutView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    if (load() === null) api.scoutSuggested().then((s) => (setList(s.repos), save(s.repos)), () => undefined);
  }, []);

  const update = (next: string[]) => (setList(next), save(next));
  const addCandidates = () => {
    const names = draft
      .split(/[\s,]+/)
      .map((n) => n.trim().replace(/^https:\/\/github\.com\//, "").replace(/\/$/, ""))
      .filter((n) => REPO.test(n) && !list.includes(n));
    if (names.length) update([...list, ...names]);
    setDraft("");
  };
  const run = () => {
    setBusy(true);
    setError(null);
    api.scout(list).then(
      (v) => (setView(v), setBusy(false)),
      (e: Error) => (setError(e.message), setBusy(false)),
    );
  };
  const add = (repo: string) => {
    setAdding(repo);
    api.addRepo(repo, "contributor").then(
      () => {
        setView((v) => v && { ...v, rows: v.rows.map((r) => (r.repo === repo ? { ...r, watched: true } : r)) });
        setAdding(null);
        onAdded();
      },
      (e: Error) => (setError(e.message), setAdding(null)),
    );
  };

  return (
    <section className="find-repos">
      <header className="page-head">
        <div>
          <h2>Find repos to add</h2>
          <p className="muted">
            Scout repos before you watch them: how outside contributors' PRs fared in the last 90 days, how fast maintainers
            answer, how well each fits you, open starter issues, and what the contributing guide says about a CLA and AI. Public
            GitHub data only: no model calls, nothing set up until you add one.
          </p>
        </div>
        <div className="compare-actions">
          <button className="primary" onClick={run} disabled={busy || list.length === 0}>
            {busy ? `Scouting ${list.length} repos…` : `Scout ${list.length} repos`}
          </button>
        </div>
      </header>
      <div className="scout-list">
        {list.map((n) => (
          <span key={n} className="scout-chip">
            {n}
            <button className="link" aria-label={`Remove ${n}`} onClick={() => update(list.filter((x) => x !== n))}>×</button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addCandidates()}
          placeholder="owner/name, or paste several"
          aria-label="Add candidate repos"
        />
        <button className="small" onClick={addCandidates} disabled={!draft.trim()}>Add</button>
        <button className="small link" onClick={() => api.scoutSuggested().then((s) => update(s.repos))}>Reset to suggestions</button>
      </div>
      {busy && <p className="dim">The first scout reads up to 400 PRs per repo: about 10 seconds a repo. Repeats within a few hours are instant.</p>}
      {error && <p className="error">{error}</p>}
      {view && (
        <>
          {view.fit_error && <p className="dim">{view.fit_error}</p>}
          <div className="table-wrap">
            <table className="grid compare-grid scout-grid">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Repo</th>
                  <th title="How much of the repo's code is in languages you write">Fit</th>
                  <th>Outside PRs, 90 days</th>
                  <th>First answer</th>
                  <th title="Outside PRs waiting a week or more with no maintainer response">Unanswered</th>
                  <th title="Open issues labelled good first issue · help wanted">Starters</th>
                  <th title="The contributing guide asks for a Contributor License Agreement">CLA</th>
                  <th>AI</th>
                  <th title="Repository size on GitHub: a rough guide to setup time and disk">Size</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {view.rows.map((r, i) => (
                  <Row key={r.repo} r={r} n={i + 1} open={open === r.repo} onToggle={() => setOpen(open === r.repo ? null : r.repo)}
                    adding={adding === r.repo} onAdd={() => add(r.repo)} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="dim small-print">
            Ranked by outside merge rate, then fit; inactive and archived repos last. Scouted {ago(view.checked_at)}. Org members
            who keep their membership private count as outside contributors, so a company repo can look friendlier than it is.
            PRs landed by an internal sync (Google's Copybara) count as merged. Adding a repo sets it up in the sandbox.
          </p>
        </>
      )}
    </section>
  );
}

function Row({ r, n, open, onToggle, adding, onAdd }: {
  r: ScoutRow; n: number; open: boolean; onToggle: () => void; adding: boolean; onAdd: () => void;
}) {
  if (r.error) {
    return (
      <tr>
        <td />
        <td>{r.repo}</td>
        <td colSpan={9} className="error">{r.error}</td>
      </tr>
    );
  }
  const p = r.pulse;
  const decided = p ? p.outside_prs_merged + p.outside_prs_closed_unmerged : 0;
  const action = r.watched ? (
    <span className="dim">Watching</span>
  ) : (
    <button className="small" onClick={(e) => (e.stopPropagation(), onAdd())} disabled={adding}>{adding ? "Adding…" : "Add"}</button>
  );
  return (
    <>
      <tr className={`compare-row${open ? " open" : ""}`} onClick={onToggle} aria-expanded={open}>
        <td className="mono dim">{n}</td>
        <td title={r.description}>
          <span className="compare-repo">{r.repo}</span>
          {r.archived ? <span className="verdict verdict-inactive">archived</span> : p && <span className={`verdict verdict-${p.verdict}`}>{p.verdict}</span>}
        </td>
        <td title={r.fit?.why}>{r.fit ? <span className={`fit fit-${r.fit.label}`}>{r.fit.label}</span> : <span className="dim">–</span>}</td>
        <td className="mono">
          {p && decided ? (
            <>
              {p.outside_prs_merged} of {decided} merged
              <span className="dim"> · {Math.round((p.merge_rate ?? 0) * 100)}%</span>
            </>
          ) : <span className="dim">none decided</span>}
          {r.covers_days != null && r.covers_days < 90 && <span className="dim" title="The PRs read reach back only this far"> · {r.covers_days} d</span>}
        </td>
        <td className="mono">{p?.median_pr_response_days == null ? "–" : `${p.median_pr_response_days} d`}</td>
        <td className={`mono${p && p.outside_prs_waiting >= 10 ? " warn-text" : ""}`}>{p ? p.outside_prs_waiting : "–"}</td>
        <td className="mono">{r.good_first ?? 0} · {r.help_wanted ?? 0}</td>
        <td>{r.cla ? <span className="ai-chip" title="Sign the CLA before your first PR can be merged">CLA</span> : <span className="dim">{r.guide ? "no" : "–"}</span>}</td>
        <td>{r.ai_policy?.length ? <span className="ai-chip" title={r.ai_policy.join("\n")}>says</span> : <span className="dim">{r.guide ? "silent" : "–"}</span>}</td>
        <td className="mono dim">{r.size_mb != null ? (r.size_mb >= 1024 ? `${(r.size_mb / 1024).toFixed(1)} GB` : `${r.size_mb} MB`) : "–"}</td>
        <td>{action}</td>
      </tr>
      {open && (
        <tr className="compare-detail">
          <td />
          <td colSpan={10}>
            {r.description && <p>{r.description}</p>}
            {p && <p className="muted">{p.verdict_reason}</p>}
            <p className="dim">
              {r.language ?? "No main language"} · {(r.stars ?? 0).toLocaleString()} stars
              {r.fit && ` · fit: ${r.fit.why}`}
              {p?.last_merge && ` · last merge ${p.last_merge}`}
            </p>
            {r.ai_policy && r.ai_policy.length > 0 && (
              <div className="ai-rules">
                <b>The contributing guide on AI</b>
                <ul>{r.ai_policy.map((x) => <li key={x}>{x}</li>)}</ul>
              </div>
            )}
            {!r.guide && <p className="dim">No CONTRIBUTING.md found: read the README before you plan anything.</p>}
            <a className="small-link" href={`https://github.com/${r.repo}/issues?q=is%3Aopen+label%3A%22good+first+issue%22`} target="_blank" rel="noreferrer">
              Starter issues on GitHub →
            </a>
          </td>
        </tr>
      )}
    </>
  );
}
