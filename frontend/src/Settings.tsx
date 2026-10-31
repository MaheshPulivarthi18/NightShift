import { useState } from "react";
import { api, ImageUsage, Overview } from "./api";
import { money, usePoll } from "./ui";

// Run control and spend. Budgets, models and the rest are set in .env (see docs/SETUP.md).

export type Theme = "auto" | "light" | "dark";

export function applyTheme(theme: Theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("ns-theme", theme);
  } catch {
    // private windows may refuse storage: the theme still applies for this visit
  }
}

export function savedTheme(): Theme {
  try {
    const t = localStorage.getItem("ns-theme");
    return t === "light" || t === "dark" ? t : "auto";
  } catch {
    return "auto";
  }
}

const gb = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;

/** Sandbox images on disk, per repo, and a clean-up that keeps what work still needs. */
function Images() {
  const [use, , reload] = usePoll<ImageUsage>(api.images, 60000, []);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const clean = () => {
    setBusy(true);
    setMsg(null);
    api.pruneImages().then(
      (r) => (setMsg(`Removed ${r.removed?.length ?? 0} images.`), reload()),
      (e: Error) => setMsg(e.message),
    ).finally(() => setBusy(false));
  };
  return (
    <section className="panel">
      <div className="panel-h">
        <h2>Sandbox images</h2>
        {use && !use.error && <span className="dim">{gb(use.total)} of their own, beside shared layers</span>}
      </div>
      <div className="panel-b">
        {use?.error && <p className="dim">{use.error}</p>}
        {use && !use.error && (
          <>
            <p className="dim">
              Each repo keeps its newest {use.keep} images; older ones go after each setup. Clean up also removes
              environment images no repo needs and images of removed repos. Sizes are what removing each image frees.
            </p>
            <table className="grid">
              <thead><tr><th>Repo</th><th className="right">Images</th><th className="right">Newest</th><th className="right">Together</th></tr></thead>
              <tbody>
                {use.repos.map((r) => (
                  <tr key={r.repo}>
                    <td className="mono">{r.repo}</td>
                    <td className="right">{r.images}</td>
                    <td className="right">{gb(r.newest)}</td>
                    <td className="right">{gb(r.size)}</td>
                  </tr>
                ))}
                {use.other.images > 0 && (
                  <tr><td className="dim">Environments, checkpoints and removed repos</td><td className="right">{use.other.images}</td><td /><td className="right">{gb(use.other.size)}</td></tr>
                )}
              </tbody>
            </table>
            <div className="row-flex">
              <button className="ghost small" disabled={busy} onClick={clean}>{busy ? "Cleaning up…" : "Clean up"}</button>
              {use.removable > 0 && <span className="dim">{use.removable} repo images are past the newest {use.keep}.</span>}
              {msg && <span className="dim">{msg}</span>}
            </div>
          </>
        )}
      </div>
    </section>
  );
}

export function Settings() {
  const [ov, , reload] = usePoll<Overview>(api.overview, 15000, []);
  const [armed, setArmed] = useState(false);
  const toggle = (killed: boolean) => api.setKill(killed).then(() => (setArmed(false), reload()));
  return (
    <div className="page settings">
      <header className="page-head"><h1>Settings</h1></header>
      <section className="panel">
        <div className="panel-h">
          <h2>Run control</h2>
          {ov && <span className="status"><span className={`dot ${ov.killed ? "bad" : "ok"}`} />{ov.killed ? "All model calls blocked" : "Running"}</span>}
        </div>
        <div className="panel-b row-flex">
          <span className="grow dim">The kill switch blocks every model call at once, including runs in progress. Nothing else changes; resume when you are ready.</span>
          {ov?.killed ? (
            <button className="primary small" onClick={() => toggle(false)}>Resume</button>
          ) : armed ? (
            <>
              <button className="danger small" onClick={() => toggle(true)}>Block all model calls</button>
              <button className="ghost small" onClick={() => setArmed(false)}>Cancel</button>
            </>
          ) : (
            <button className="ghost small danger-text" onClick={() => setArmed(true)}>Kill switch</button>
          )}
        </div>
      </section>
      <section className="panel">
        <div className="panel-h"><h2>Spend</h2></div>
        <div className="panel-b">
          {ov ? (
            <div className="stats flat">
              <div className="stat"><div className="v">{money(ov.spend.today)}</div><div className="l">Today</div></div>
              <div className="stat"><div className="v">{money(ov.spend.last_7_days)}</div><div className="l">Last 7 days</div></div>
              <div className="stat"><div className="v">{money(ov.spend.this_month)}</div><div className="l">This month</div></div>
            </div>
          ) : <p className="dim">Loading…</p>}
          <p className="dim">Daily and monthly caps, and per-task limits, are set in <span className="mono">.env</span> and checked before every model call. See <span className="mono">docs/SETUP.md</span>.</p>
        </div>
      </section>
      <Images />
      <section className="panel">
        <div className="panel-h"><h2>Advanced</h2></div>
        <div className="panel-b links">
          <a href="#/models">Models: which model each agent uses, and how each has done</a>
          <a href="#/jobs">Jobs: every run, with its live log and cost</a>
          <a href="#/board">All tasks on one board</a>
          <a href="#/checks">All PR checks</a>
          <a href="#/repos">Manage repos: add, remove, contributor or maintainer view, check new PRs</a>
        </div>
      </section>
    </div>
  );
}
