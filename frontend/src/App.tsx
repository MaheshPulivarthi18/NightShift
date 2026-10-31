import { FormEvent, useEffect, useState } from "react";
import { api, Overview, RepoRow, TaskSummary } from "./api";
import { Board } from "./Board";
import { Chat } from "./Chat";
import { Compare } from "./Compare";
import { TrackRecord } from "./TrackRecord";
import { Home, needsYou } from "./Home";
import { Job, Jobs } from "./Jobs";
import { Models } from "./Models";
import { PRCheckPage, PRChecks } from "./PRChecks";
import { Report, Repos } from "./Repos";
import { applyTheme, savedTheme, Settings, Theme } from "./Settings";
import { TaskPage } from "./TaskPage";
import { money, usePoll } from "./ui";
import { Workspace, WorkspaceTab } from "./Workspace";

// Hash routes, so the server only serves one page:
//   #/  overview · #/compare  where to contribute · #/r/o/r/<tab>[/t12 | /c3]  a repo's workspace · #/settings
//   older pages, under Settings > Advanced: #/board · #/tasks/12 · #/repos · #/repos/o/r/report · #/checks ·
//   #/checks/3 · #/jobs · #/jobs/<id> · #/models · #/chat/o/r/42
function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

interface Route { page: string; repo?: string; full?: boolean; el: JSX.Element }

function route(hash: string): Route {
  const [path, query = ""] = hash.slice(1).split("?");
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "r" && parts.length >= 3) {
    const repo = `${parts[1]}/${parts[2]}`;
    const tab = (["overview", "items", "map", "knowledge"].includes(parts[3]) ? parts[3] : "items") as WorkspaceTab;
    return { page: "repo", repo, full: true, el: <Workspace repo={repo} tab={tab} sel={parts[4] ?? null} /> };
  }
  if (parts[0] === "settings") return { page: "settings", el: <Settings /> };
  if (parts[0] === "record") return { page: "record", el: <TrackRecord /> };
  if (parts[0] === "compare")
    return { page: "compare", el: <Compare initial={new URLSearchParams(query).get("open")} /> };
  if (parts[0] === "tasks" && parts[1]) return { page: "", el: <TaskPage id={Number(parts[1])} /> };
  if (parts[0] === "board" || parts[0] === "tasks") {
    const q = new URLSearchParams(query);
    return { page: "", el: <Board prefillRepo={q.get("repo") ?? ""} prefillIssue={q.get("issue") ?? ""} /> };
  }
  if (parts[0] === "repos" && parts.length === 4 && parts[3] === "report")
    return { page: "repo", repo: `${parts[1]}/${parts[2]}`, el: <Report repo={`${parts[1]}/${parts[2]}`} /> };
  if (parts[0] === "repos") return { page: "", el: <Repos /> };
  if (parts[0] === "jobs" && parts[1]) return { page: "", el: <Job id={parts[1]} /> };
  if (parts[0] === "jobs") return { page: "", el: <Jobs /> };
  if (parts[0] === "models") return { page: "", el: <Models /> };
  if (parts[0] === "checks" && parts[1]) return { page: "", el: <PRCheckPage id={Number(parts[1])} /> };
  if (parts[0] === "checks") return { page: "", el: <PRChecks /> };
  if (parts[0] === "chat" && parts.length === 4)
    return { page: "repo", repo: `${parts[1]}/${parts[2]}`, el: <Chat repo={`${parts[1]}/${parts[2]}`} number={Number(parts[3])} /> };
  return { page: "home", el: <Home /> };
}

export function App() {
  const r = route(useHash());
  return (
    <div className="app">
      <Sidebar page={r.page} repo={r.repo} />
      <main className={r.full ? "main full" : "main"}><OldCode />{r.el}</main>
    </div>
  );
}

/** Changes need a restart of `nightshift serve`, and nothing else on the page shows it. */
function OldCode() {
  const [v] = usePoll(api.version, 60000, []);
  if (!v?.old) return null;
  return (
    <div className="old-code" role="status">
      NightShift was updated ({v.running?.slice(0, 7)} → {v.latest?.slice(0, 7)}) but this server still runs the old code.
      Restart <code>nightshift serve</code> to use it.
    </div>
  );
}

const SEARCH_FROM = 8; // repos in the sidebar before it gets a search box

function Sidebar({ page, repo }: { page: string; repo?: string }) {
  const [repos, , reloadRepos] = usePoll<RepoRow[]>(api.repos, 30000, []);
  const [tasks] = usePoll<TaskSummary[]>(() => api.tasks(), 15000, []);
  const [ov] = usePoll<Overview>(api.overview, 15000, []);
  const [theme, setTheme] = useState<Theme>(savedTheme);
  useEffect(() => applyTheme(theme), [theme]);
  const needs = needsYou(tasks ?? []);
  const [find, setFind] = useState("");
  const work = (name: string) => needs.filter((t) => t.repo === name).length;
  // repos that need you first, then by name; the search matches owner or name
  const shown = (repos ?? [])
    .filter((r) => r.full_name.toLowerCase().includes(find.trim().toLowerCase()))
    .sort((a, b) => Number(work(b.full_name) > 0) - Number(work(a.full_name) > 0) || a.full_name.split("/")[1].localeCompare(b.full_name.split("/")[1]));
  return (
    <nav className="side" aria-label="Main">
      <a className="brand" href="#/"><span className="mark" aria-hidden><i /></span>NightShift</a>
      <a className={`nav${page === "home" ? " on" : ""}`} href="#/">
        Overview<span className={`count${needs.length ? " attn" : ""}`}>{needs.length || ""}</span>
      </a>
      <a className={`nav${page === "compare" ? " on" : ""}`} href="#/compare">Where to contribute</a>
      <a className={`nav${page === "record" ? " on" : ""}`} href="#/record">Track record</a>
      <a className={`nav${page === "settings" ? " on" : ""}`} href="#/settings">Settings</a>
      <div className="side-label">Repos</div>
      {(repos?.length ?? 0) >= SEARCH_FROM && (
        <input className="side-find" type="search" placeholder="Find a repo" aria-label="Find a repo" value={find}
          onChange={(e) => setFind(e.target.value)} onKeyDown={(e) => e.key === "Escape" && setFind("")} />
      )}
      {find && shown.length === 0 && <div className="side-none dim">No repo matches.</div>}
      {shown.map((r) => {
        const n = work(r.full_name);
        const name = r.full_name.split("/")[1];
        return (
          <a key={r.full_name} className={`nav${page === "repo" && repo === r.full_name ? " on" : ""}`} href={`#/r/${r.full_name}/items`} title={r.full_name}>
            <span className="av" aria-hidden>{name[0].toUpperCase()}</span>
            <span className="name">{name}</span>
            <span className={`count${n ? " attn" : ""}`}>{n || ""}</span>
          </a>
        );
      })}
      <AddRepo onAdded={reloadRepos} />
      <div className="side-foot">
        {ov && (
          <span className={ov.killed ? "danger-text" : "dim"}>
            {ov.killed ? "Model calls blocked · " : ""}today {money(ov.spend.today)} · month {money(ov.spend.this_month)}
          </span>
        )}
        <div className="seg" role="group" aria-label="Theme">
          {(["auto", "light", "dark"] as const).map((t) => (
            <button key={t} className={theme === t ? "on" : ""} onClick={() => setTheme(t)}>{t === "auto" ? "Auto" : t === "light" ? "Light" : "Dark"}</button>
          ))}
        </div>
      </div>
    </nav>
  );
}

function AddRepo({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    api.addRepo(name.trim(), "contributor").then(
      () => {
        const full = name.trim().replace(/^https:\/\/github\.com\//, "").replace(/\/$/, "");
        setName(""), setOpen(false), setError(null), onAdded();
        window.location.hash = `#/r/${full}/knowledge`;
      },
      (err: Error) => setError(err.message),
    );
  };
  if (!open) return <button className="nav add" onClick={() => setOpen(true)}>+ Add repo</button>;
  return (
    <form className="add-repo" onSubmit={submit}>
      <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="owner/name or URL" aria-label="Repo to add" />
      <button className="primary small">Add</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}
