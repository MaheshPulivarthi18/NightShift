import { FormEvent, ReactNode, useState } from "react";
import { api, RepoItem, RepoKnowledge } from "./api";
import { SyncBar } from "./Home";
import { MapCanvas } from "./MapCanvas";
import { PRCheckPage } from "./PRChecks";
import { TaskPage } from "./TaskPage";
import { ago, Title, usePoll } from "./ui";

// One repo, one workspace: its issues and PRs, its map and what NightShift knows about it.
// Routes: #/r/<owner>/<name>/<tab>[/t<task id> | /c<check id>]

const TABS = [["overview", "Overview"], ["items", "Issues & PRs"], ["map", "Map"], ["knowledge", "Knowledge"]] as const;
export type WorkspaceTab = (typeof TABS)[number][0];

export function Workspace({ repo, tab, sel }: { repo: string; tab: WorkspaceTab; sel: string | null }) {
  const [owner, name] = repo.split("/");
  return (
    <div className="ws">
      <header className="ws-head">
        <div className="crumb">
          <span className="dim">{owner}</span><span className="dim">/</span><b>{name}</b>
          <a className="dim" href={`https://github.com/${repo}`} target="_blank" rel="noreferrer">GitHub ↗</a>
          <span className="push"><SyncBar /></span>
        </div>
        <nav className="tabs" role="tablist">
          {TABS.map(([key, label]) => (
            <a key={key} role="tab" className={tab === key ? "on" : ""} href={`#/r/${repo}/${key}`}>{label}</a>
          ))}
        </nav>
      </header>
      {tab === "items" && <Items repo={repo} sel={sel} />}
      {tab === "overview" && <RepoOverview repo={repo} />}
      {tab === "map" && <RepoMapTab repo={repo} />}
      {tab === "knowledge" && <Knowledge repo={repo} />}
    </div>
  );
}

// ---------- Issues & PRs ----------

const ROLE = { yours: "Yours", incoming: "Incoming PR", issue: "Issue" } as const;

/** Where the item stands, in words, and the tone of its dot: info waits on you, bad needs work. */
function itemStatus(i: RepoItem): [string, string] {
  if (i.pr_state === "MERGED") return ["Merged", "ok"];
  if (i.pr_state === "CLOSED") return ["Closed without merge", ""];
  if (i.kind === "check") {
    if (i.status === "running") return ["Checking", "info"];
    if (i.status !== "done") return ["Check stopped", "bad"];
    return [`Checked · ${i.verdict ?? ""}`, i.must ? "bad" : i.should ? "warn" : "ok"];
  }
  if (i.asking && !i.asked) return ["Needs answers: ask the maintainers", "info"];
  if (i.asking && i.feedback) return [`Answered · ${i.feedback}: plan it again`, "info"];
  if (i.asking) return ["Question posted · waiting for a reply", ""];
  if (i.stage === "done" && i.feedback) return [`In review · ${i.feedback} new comment${i.feedback > 1 ? "s" : ""}`, "info"];
  if (i.stage === "done" && i.ci_failing?.length) return [`CI failing: ${i.ci_failing.slice(0, 3).join(", ")}`, "bad"];
  return ({
    plan: ["Planning", ""], gate1: ["Plan ready: approve it", "info"], work: ["Working", ""],
    gate2: ["Ready to publish", "info"], done: ["In review", ""], stopped: ["Stopped", "bad"], closed: ["Plan rejected", ""],
  } as Record<string, [string, string]>)[i.stage ?? ""] ?? [i.status, ""];
}

function Items({ repo, sel }: { repo: string; sel: string | null }) {
  const [items, error, reload] = usePoll(() => api.repoItems(repo), 15000, [repo]);
  const [list, setList] = useState<"open" | "closed">("open");
  const [role, setRole] = useState<"all" | RepoItem["role"]>("all");
  const [hidden, setHidden] = useState(false);
  const inList = (items ?? []).filter((i) => (list === "open") === i.open && (hidden || !i.hidden));
  const shown = inList.filter((i) => role === "all" || i.role === role);
  const count = (o: boolean) => (items ?? []).filter((i) => i.open === o && (hidden || !i.hidden)).length;
  const key = (i: RepoItem) => `${i.kind === "task" ? "t" : "c"}${i.id}`;
  return (
    <div className="work">
      <aside className="item-list">
        <div className="oc" role="tablist">
          <button className={list === "open" ? "on" : ""} onClick={() => setList("open")}>Open {count(true)}</button>
          <button className={list === "closed" ? "on" : ""} onClick={() => setList("closed")}>Closed {count(false)}</button>
          <label className="hidden-toggle"><input type="checkbox" checked={hidden} onChange={(e) => setHidden(e.target.checked)} /> Hidden</label>
        </div>
        <div className="chips" role="group" aria-label="Filter">
          {(["all", "yours", "incoming", "issue"] as const).map((r) => (
            <button key={r} className={`chip${role === r ? " on" : ""}`} onClick={() => setRole(r)}>
              {r === "all" ? "All" : r === "issue" ? "Issues" : ROLE[r]} {r === "all" ? inList.length : inList.filter((i) => i.role === r).length}
            </button>
          ))}
        </div>
        <div className="scroll">
          {error && <p className="error pad">{error}</p>}
          {items && shown.length === 0 && <p className="dim pad">Nothing here.</p>}
          {shown.map((i) => {
            const [status, tone] = itemStatus(i);
            return (
              <a key={key(i)} className={`li${sel === key(i) ? " on" : ""}`} href={`#/r/${repo}/items/${key(i)}`}>
                <span className="l1">
                  <span className="mono dim">{i.pr_number ? `PR #${i.pr_number}` : `#${i.number}`}</span>
                  <span className={`badge ${i.role}`}>{ROLE[i.role]}</span>
                  {i.pr_number && <span className="dim small-print">fixes #{i.number}</span>}
                  {i.hidden && <span className="badge">Hidden</span>}
                  <span className="dim when">{ago(i.updated_at)}</span>
                </span>
                <span className="t"><Title text={i.title} /></span>
                <span className="status"><span className={`dot ${tone}`} />{status}</span>
              </a>
            );
          })}
        </div>
        <a className="li new" href={`#/r/${repo}/items`}>+ Plan an issue or check a PR</a>
      </aside>
      <section className="item-pane">
        {sel?.startsWith("t") && <TaskPage id={Number(sel.slice(1))} embedded />}
        {sel?.startsWith("c") && <PRCheckPage id={Number(sel.slice(1))} embedded />}
        {!sel && <Start repo={repo} onStarted={reload} />}
      </section>
    </div>
  );
}

function Start({ repo, onStarted }: { repo: string; onStarted: () => void }) {
  return (
    <div className="pane-pad start">
      <h1>Start something in {repo.split("/")[1]}</h1>
      <p className="dim">Pick an item on the left, or start one here. Nothing is posted to GitHub without you.</p>
      <StartForm
        title="Plan a fix for an issue"
        hint="NightShift reads the issue and the code, plans the fix and checks the plan against the code. You approve it before anything runs."
        placeholder="Issue number, e.g. 123"
        note
        submit="Plan it"
        run={(v, note) => api.create(repo, Number(v.replace("#", "")), "", note).then(() => "Planning started; it shows on the left in a moment.")}
        onDone={onStarted}
      />
      <StartForm
        title="Check an incoming pull request"
        hint="Checks its template and evidence, runs it on the merge preview, and reviews it. Nothing is posted."
        placeholder={`https://github.com/${repo}/pull/45`}
        submit="Check it"
        run={(v) => api.checkPr(v).then(() => "Checking; it shows on the left in a moment.")}
        onDone={onStarted}
      />
      <StartForm
        title="Follow up a pull request of yours"
        hint="For a PR you opened by hand: NightShift reads its review comments and helps you answer or address them."
        placeholder={`https://github.com/${repo}/pull/67`}
        submit="Follow it"
        run={(v) => api.fromPr(v).then((t) => ((window.location.hash = `#/r/${repo}/items/t${t.id}`), ""))}
        onDone={onStarted}
      />
    </div>
  );
}

function StartForm(props: {
  title: string; hint: string; placeholder: string; submit: string; note?: boolean;
  run: (value: string, note: string) => Promise<string>; onDone: () => void;
}) {
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    props.run(value.trim(), note.trim()).then(
      (m) => (setMsg(m || null), setValue(""), setNote(""), window.setTimeout(props.onDone, 1500)),
      (err: Error) => setMsg(err.message),
    ).finally(() => setBusy(false));
  };
  return (
    <form className="panel start-form" onSubmit={submit}>
      <h2>{props.title}</h2>
      <p className="dim">{props.hint}</p>
      <div className="inline-form">
        <input value={value} onChange={(e) => setValue(e.target.value)} placeholder={props.placeholder} required />
        <button className="primary" disabled={busy}>{busy ? "Starting…" : props.submit}</button>
      </div>
      {props.note && (
        <input className="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note to the Planner, e.g. treat it as a docs change" />
      )}
      {msg && <p className="dim">{msg}</p>}
    </form>
  );
}

// ---------- Overview, Map, Knowledge ----------

function RepoOverview({ repo }: { repo: string }) {
  const [stats, statsError] = usePoll(() => api.repoStats(repo), 3_600_000, [repo]);
  const [repos] = usePoll(api.repos, 30000, []);
  const row = repos?.find((r) => r.full_name === repo);
  const n = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString());
  return (
    <div className="pane-pad">
      <div className="stats">
        <Stat value={n(stats?.open_issues)} label="Open issues" />
        <Stat value={n(stats?.open_prs)} label="Open PRs" />
        <Stat value={n(stats?.merged_30d)} label="Merged, last 30 days" />
        <Stat value={n(stats?.stars)} label="Stars" />
      </div>
      {statsError && <p className="dim">GitHub numbers are not available right now: {statsError}</p>}
      <section className="panel">
        <div className="panel-h"><h2>Worth contributing?</h2>{row?.verdict && <span className={`verdict verdict-${row.verdict}`}>{row.verdict}</span>}</div>
        <div className="panel-b">
          {row?.report_at ? (
            <p>From the health report of {ago(row.report_at)}. <a href={`#/repos/${repo}/report`}>Open the report</a></p>
          ) : (
            <p className="dim">No health report yet. It shows how fast maintainers reply and how many outside PRs get merged.</p>
          )}
          <button className="ghost small" onClick={() => api.runReport(repo)}>{row?.report_at ? "Run it again" : "Run a health report"}</button>
        </div>
      </section>
    </div>
  );
}

function Stat({ value, label }: { value: ReactNode; label: string }) {
  return <div className="stat"><div className="v">{value}</div><div className="l">{label}</div></div>;
}

function RepoMapTab({ repo }: { repo: string }) {
  const [map, error] = usePoll(() => api.repoMap(repo), 3_600_000, [repo]);
  if (map) return <MapCanvas map={map} repo={repo} />;
  return (
    <div className="pane-pad">
      {error ? (
        <section className="panel"><div className="panel-b">
          <h2>No map for {repo} yet</h2>
          <p className="dim">The map shows the repo's components, the files behind them and how they connect. It is built when NightShift first plans a fix here, or with <span className="mono">nightshift context map {repo}</span>.</p>
        </div></section>
      ) : <p className="dim">Loading…</p>}
    </div>
  );
}

function Knowledge({ repo }: { repo: string }) {
  const [k, error, reload] = usePoll<RepoKnowledge>(() => api.repoKnowledge(repo), 5000, [repo]);
  if (error) return <p className="error pane-pad">{error}</p>;
  if (!k) return <p className="dim pane-pad">Loading…</p>;
  const env = k.environment;
  return (
    <div className="pane-pad knowledge">
      <p className="dim">Learned once when the repo is added, and reused by every issue and PR in it.</p>
      <SetupProgress repo={repo} setup={k.setup} onStarted={reload} />
      <section className="panel">
        <div className="panel-h"><h2>Environment</h2>{env ? <span className={`badge ${env.status === "ready" ? "ok" : "must"}`}>{env.status}</span> : <span className="badge">not set up</span>}</div>
        <div className="panel-b">
          {env ? (
            <>
              <p className="dim">Proven in the sandbox at <span className="mono">{env.commit.slice(0, 7)}</span>, {ago(env.at)}. {env.detail}</p>
              <Commands title="Setup" list={env.setup_commands} />
              <Commands title="Tests" list={env.tests.map((t) => `${t.ok ? "passes" : "fails"} · ${t.command}`)} />
              {env.coverage && <p className={`small-print ${env.coverage.includes("whole suite") ? "dim" : "warn-text"}`}>{env.coverage}</p>}
            </>
          ) : (
            <>
              <p className="dim">Not set up yet. The Setup agent works out how to install and test the repo in the sandbox.</p>
            </>
          )}
        </div>
      </section>
      {k.recipe && (
        <section className="panel">
          <div className="panel-h"><h2>Commands that worked in a fix</h2></div>
          <div className="panel-b"><Commands title="Setup" list={k.recipe.setup} /><Commands title="Tests" list={k.recipe.tests} /></div>
        </section>
      )}
      <section className="panel">
        <div className="panel-h"><h2>Code map</h2>{k.map && <a className="push" href={`#/r/${repo}/map`}>Open the map</a>}</div>
        <div className="panel-b">
          {k.map ? (
            <p className="dim">{k.map.components} components and {k.map.relationships} links, built at <span className="mono">{k.map.commit.slice(0, 7)}</span>, {ago(k.map.built_at)}.</p>
          ) : <p className="dim">Not built yet.</p>}
        </div>
      </section>
      <section className="panel">
        <div className="panel-h"><h2>Repo brief and PR rules</h2></div>
        <div className="panel-b">{k.brief ? <pre className="brief">{k.brief}</pre> : <p className="dim">Written during setup, from the repo's README and contributing guide.</p>}</div>
      </section>
    </div>
  );
}

const STEPS = [["environment", "Environment"], ["map", "Code map"], ["brief", "Brief and PR rules"]] as const;

/** The run that sets the repo up, step by step, with its log and a way to run it again. */
function SetupProgress({ repo, setup, onStarted }: { repo: string; setup: RepoKnowledge["setup"]; onStarted: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const start = () => api.onboardRepo(repo).then(() => (setError(null), onStarted()), (e: Error) => setError(e.message));
  const running = setup?.status === "running";
  const at = STEPS.findIndex(([key]) => key === setup?.step);
  return (
    <section className="panel">
      <div className="panel-h">
        <h2>Setup</h2>
        {setup && (
          <span className="status">
            <span className={`dot ${running ? "info" : setup.status === "done" ? "ok" : setup.status === "interrupted" ? "warn" : "bad"}`} />
            {running ? "Setting up…" : setup.status === "done" ? `Set up ${ago(setup.at)}`
              : setup.status === "interrupted" ? "Interrupted: NightShift was closed during setup" : `Finished with problems, ${ago(setup.at)}`}
          </span>
        )}
        <span className="push row-flex">
          {setup && <a className="small-print" href={`#/jobs/${setup.run_id}`}>Live log</a>}
          <button className="ghost small" onClick={start} disabled={running}>{setup ? "Set up again" : "Set it up"}</button>
        </span>
      </div>
      <div className="panel-b">
        {setup ? (
          <ol className="steps-row">
            {STEPS.map(([key, label], i) => {
              const state = !running && setup.status === "done" ? "done" : i < at ? "done" : i === at ? (running ? "now" : "stopped") : "";
              return <li key={key} className={state}>{state === "done" ? "✓ " : ""}{label}</li>;
            })}
          </ol>
        ) : (
          <p className="dim">Not set up yet: environment, code map and brief are learned in one run.</p>
        )}
        {setup?.error && !running && <p className="dim">{setup.error}</p>}
        {error && <p className="error">{error}</p>}
      </div>
    </section>
  );
}

function Commands({ title, list }: { title: string; list: string[] }) {
  if (!list.length) return null;
  return (
    <>
      <h3>{title}</h3>
      <ul className="cmds">{list.map((c) => <li key={c} className="mono">{c}</li>)}</ul>
    </>
  );
}
