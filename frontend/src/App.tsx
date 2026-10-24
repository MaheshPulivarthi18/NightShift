import { useEffect, useState } from "react";
import { api, Overview } from "./api";
import { Board } from "./Board";
import { Chat } from "./Chat";
import { Job, Jobs } from "./Jobs";
import { Report, Repos } from "./Repos";
import { TaskPage } from "./TaskPage";
import { money } from "./ui";

// Hash routes, so the server only serves one page:
//   #/  tasks · #/tasks/12 · #/repos · #/repos/o/r/report · #/jobs · #/jobs/<id> · #/chat/o/r/42
function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => {
      setHash(window.location.hash || "#/");
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

function route(hash: string): { page: string; el: JSX.Element } {
  const [path, query = ""] = hash.slice(1).split("?");
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "tasks" && parts[1]) return { page: "tasks", el: <TaskPage id={Number(parts[1])} /> };
  if (parts[0] === "repos" && parts.length === 4 && parts[3] === "report")
    return { page: "repos", el: <Report repo={`${parts[1]}/${parts[2]}`} /> };
  if (parts[0] === "repos") return { page: "repos", el: <Repos /> };
  if (parts[0] === "jobs" && parts[1]) return { page: "jobs", el: <Job id={parts[1]} /> };
  if (parts[0] === "jobs") return { page: "jobs", el: <Jobs /> };
  if (parts[0] === "chat" && parts.length === 4)
    return { page: "repos", el: <Chat repo={`${parts[1]}/${parts[2]}`} number={Number(parts[3])} /> };
  const q = new URLSearchParams(query);
  return { page: "tasks", el: <Board prefillRepo={q.get("repo") ?? ""} prefillIssue={q.get("issue") ?? ""} /> };
}

export function App() {
  const { page, el } = route(useHash());
  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/">
          <span className="lamp" aria-hidden />
          NightShift
        </a>
        <nav>
          <a href="#/" className={page === "tasks" ? "active" : ""}>Tasks</a>
          <a href="#/repos" className={page === "repos" ? "active" : ""}>Repos</a>
          <a href="#/jobs" className={page === "jobs" ? "active" : ""}>Jobs</a>
        </nav>
        <Spend />
      </header>
      <main>{el}</main>
    </div>
  );
}

/** Model spend and the kill switch: always in view, because they guard the budget. */
function Spend() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    const load = () => api.overview().then(setOv, () => undefined);
    load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, []);
  if (!ov) return <div className="spend" />;
  const toggle = (killed: boolean) => api.setKill(killed).then((o) => (setOv(o), setArmed(false)));
  return (
    <div className="spend">
      <span className="mono dim">
        today {money(ov.spend.today)} · month {money(ov.spend.this_month)}
      </span>
      {ov.killed ? (
        <button className="danger small" onClick={() => toggle(false)} title="Model calls are blocked">
          Kill switch on · resume
        </button>
      ) : armed ? (
        <>
          <button className="danger small" onClick={() => toggle(true)}>Block all model calls</button>
          <button className="ghost small" onClick={() => setArmed(false)}>Cancel</button>
        </>
      ) : (
        <button className="ghost small" onClick={() => setArmed(true)}>Kill switch</button>
      )}
    </div>
  );
}
