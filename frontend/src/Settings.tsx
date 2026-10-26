import { useState } from "react";
import { api, Overview } from "./api";
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
      <section className="panel">
        <div className="panel-h"><h2>Advanced</h2></div>
        <div className="panel-b links">
          <a href="#/models">Models: which model each agent uses, and how each has done</a>
          <a href="#/jobs">Jobs: every run, with its live log and cost</a>
          <a href="#/board">All tasks on one board</a>
          <a href="#/checks">All PR checks</a>
          <a href="#/repos">Manage repos: add, remove, report view</a>
        </div>
      </section>
    </div>
  );
}
